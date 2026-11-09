package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const logCapacity = 2000

// LogHub хранит последние строки журнала и раздаёт новые подписчикам SSE.
type LogHub struct {
	mu    sync.Mutex
	lines []string
	subs  map[chan string]struct{}
}

func newLogHub() *LogHub { return &LogHub{subs: map[chan string]struct{}{}} }

func (h *LogHub) Add(line string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.lines = append(h.lines, line)
	if len(h.lines) > logCapacity {
		h.lines = h.lines[len(h.lines)-logCapacity:]
	}
	for sub := range h.subs {
		select {
		case sub <- line:
		default: // медленный клиент теряет строку, но не задерживает перевод
		}
	}
}

func (h *LogHub) Subscribe() ([]string, chan string, func()) {
	h.mu.Lock()
	defer h.mu.Unlock()
	ch := make(chan string, 256)
	h.subs[ch] = struct{}{}
	backlog := append([]string(nil), h.lines...)
	return backlog, ch, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		delete(h.subs, ch)
	}
}

// JobState — состояние запущенной задачи для интерфейса.
type JobState struct {
	Running   bool   `json:"running"`
	Label     string `json:"label,omitempty"`
	StartedAt int64  `json:"startedAt,omitempty"`
	// Итог последней задачи: "", "ok", "failed", "stopped".
	Outcome string `json:"outcome,omitempty"`
	Message string `json:"message,omitempty"`
}

var errJobRunning = errors.New("уже идёт другая задача")

// Runner запускает команды CLI по одной: две задачи одновременно боролись бы за видеопамять.
type Runner struct {
	root, python string
	hub          *LogHub

	mu      sync.Mutex
	state   JobState
	cmd     *exec.Cmd
	stopped bool
	lastErr string
}

func NewRunner(root, python string, hub *LogHub) *Runner {
	return &Runner{root: root, python: python, hub: hub}
}

func (r *Runner) State() JobState {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.state
}

func (r *Runner) command(ctx context.Context, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, r.python, append([]string{"-m", "src.cli"}, args...)...)
	cmd.Dir = r.root
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1", "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")
	cmd.Cancel = func() error { return killTree(cmd) }
	// Дочерний Chromium может держать трубу открытой, поэтому ожидание ограничено.
	cmd.WaitDelay = 5 * time.Second
	hideWindow(cmd)
	return cmd
}

// Quick выполняет короткую команду вне очереди задач (постановка в очередь, удаление).
func (r *Runner) Quick(ctx context.Context, args ...string) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	out, err := r.command(ctx, args...).CombinedOutput()
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line = strings.TrimSpace(line); strings.Contains(line, " INFO ") || strings.Contains(line, " ERROR ") {
			r.hub.Add(line)
		}
	}
	if err != nil {
		return fmt.Errorf("%s: %w: %s", args[0], err, lastLines(string(out)))
	}
	return nil
}

// Output выполняет короткую команду и возвращает её вывод.
func (r *Runner) Output(ctx context.Context, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	out, err := r.command(ctx, args...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s: %w: %s", args[0], err, lastLines(string(out)))
	}
	return string(out), nil
}

func lastLines(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > 3 {
		lines = lines[len(lines)-3:]
	}
	return strings.Join(lines, " ")
}

// Start запускает последовательность команд; следующая идёт, только если предыдущая успешна.
func (r *Runner) Start(ctx context.Context, label string, steps ...[]string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.state.Running {
		return errJobRunning
	}
	r.state = JobState{Running: true, Label: label, StartedAt: time.Now().Unix()}
	r.stopped = false
	r.lastErr = ""
	go r.run(ctx, label, steps)
	return nil
}

func (r *Runner) run(ctx context.Context, label string, steps [][]string) {
	r.hub.Add(fmt.Sprintf("%s --- %s", time.Now().Format("15:04:05"), label))
	var runErr error
	for _, args := range steps {
		if r.isStopped() {
			break
		}
		if runErr = r.runStep(ctx, args); runErr != nil {
			break
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	r.cmd = nil
	next := JobState{Label: label, Outcome: "ok"}
	switch {
	case r.stopped:
		next.Outcome = "stopped"
		next.Message = "Перевод остановлен. Готовые куски сохранены, продолжить можно в любой момент."
	case runErr != nil:
		next.Outcome = "failed"
		next.Message = r.lastErr
		if next.Message == "" {
			next.Message = runErr.Error()
		}
	}
	r.state = next
	r.hub.Add(fmt.Sprintf("%s --- %s", time.Now().Format("15:04:05"), outcomeText(next.Outcome)))
}

func outcomeText(outcome string) string {
	switch outcome {
	case "ok":
		return "завершено"
	case "stopped":
		return "остановлено"
	}
	return "завершено с ошибкой"
}

func (r *Runner) isStopped() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stopped
}

func (r *Runner) runStep(ctx context.Context, args []string) error {
	cmd := r.command(ctx, args...)
	reader, writer := io.Pipe()
	cmd.Stdout = writer
	cmd.Stderr = writer

	r.mu.Lock()
	r.cmd = cmd
	r.mu.Unlock()

	if err := cmd.Start(); err != nil {
		r.setLastErr("Не удалось запустить Python: " + err.Error())
		return err
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
		for scanner.Scan() {
			line := scanner.Text()
			r.hub.Add(line)
			if strings.Contains(line, " ERROR ") {
				r.setLastErr(line[strings.Index(line, " ERROR ")+7:])
			}
		}
		io.Copy(io.Discard, reader) // строка длиннее буфера не должна заблокировать Python
	}()
	err := cmd.Wait()
	writer.Close()
	<-done
	return err
}

func (r *Runner) setLastErr(msg string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lastErr = msg
}

// Stop прерывает задачу. Python сохраняет каждый кусок сразу, поэтому готовое не теряется.
func (r *Runner) Stop() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.state.Running {
		return nil
	}
	r.stopped = true
	if r.cmd == nil || r.cmd.Process == nil {
		return nil
	}
	return killTree(r.cmd)
}

func killTree(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	if runtime.GOOS == "windows" {
		kill := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid))
		hideWindow(kill)
		if err := kill.Run(); err == nil {
			return nil
		}
	}
	return cmd.Process.Kill()
}

func findPython(root string) string {
	for _, candidate := range []string{
		filepath.Join(root, ".venv", "Scripts", "python.exe"),
		filepath.Join(root, ".venv", "bin", "python"),
	} {
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	return "python"
}
