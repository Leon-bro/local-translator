// Локальный веб-интерфейс переводчика: раздаёт React, запускает Python и показывает состояние.
// Перевод выполняет Python-конвейер (src/cli.py), Ollama здесь опрашивается только для статуса.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"embed"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"log"
	"mime"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

//go:embed all:dist
var distFS embed.FS

const maxUpload = 4 << 30 // 4 ГБ

var (
	hashRe      = regexp.MustCompile(`^[0-9a-f]{16}$`)
	assetRe     = regexp.MustCompile(`^[\w.-]+$`)
	badNameRe   = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]`)
	modelNameRe = regexp.MustCompile(`^[\w.:/-]+$`)
)

type server struct {
	root   string
	ctx    context.Context
	hub    *LogHub
	runner *Runner
	client *http.Client

	glossaryMu sync.Mutex // запись глоссария: чтение, изменение, сохранение
	notesMu    sync.Mutex // запись заметок читателя

	ollamaMu   sync.Mutex
	ollamaAt   time.Time
	ollamaInfo OllamaInfo
}

// OllamaInfo — ответ Ollama, кэшируется на несколько секунд.
type OllamaInfo struct {
	Up     bool     `json:"up"`
	Models []string `json:"models"`
	Loaded []string `json:"loaded"`
}

func (s *server) ollama(host string) OllamaInfo {
	s.ollamaMu.Lock()
	defer s.ollamaMu.Unlock()
	if time.Since(s.ollamaAt) < 3*time.Second {
		return s.ollamaInfo
	}
	info := OllamaInfo{Models: []string{}, Loaded: []string{}}
	var tags, ps struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := s.getJSON(host+"/api/tags", &tags); err == nil {
		info.Up = true
		for _, m := range tags.Models {
			info.Models = append(info.Models, m.Name)
		}
		slices.Sort(info.Models)
		if err := s.getJSON(host+"/api/ps", &ps); err == nil {
			for _, m := range ps.Models {
				info.Loaded = append(info.Loaded, m.Name)
			}
		}
	}
	s.ollamaInfo, s.ollamaAt = info, time.Now()
	return info
}

func (s *server) getJSON(url string, target any) error {
	resp, err := s.client.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: %s", url, resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(target)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

// lookup находит книгу по хешу в пути запроса.
func (s *server) lookup(w http.ResponseWriter, r *http.Request) (*FileInfo, *Config, bool) {
	hash := r.PathValue("hash")
	if !hashRe.MatchString(hash) {
		writeError(w, http.StatusBadRequest, "Неверный идентификатор книги")
		return nil, nil, false
	}
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return nil, nil, false
	}
	db, err := openDB(s.root, cfg)
	if err != nil {
		writeError(w, http.StatusNotFound, "Книга не найдена")
		return nil, nil, false
	}
	defer db.Close()
	files, err := listFiles(db, s.root, cfg)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return nil, nil, false
	}
	for i := range files {
		if files[i].Hash == hash {
			return &files[i], cfg, true
		}
	}
	writeError(w, http.StatusNotFound, "Книга не найдена")
	return nil, nil, false
}

func (s *server) handleState(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	files := []FileInfo{}
	if db, err := openDB(s.root, cfg); err == nil {
		defer db.Close()
		if files, err = listFiles(db, s.root, cfg); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"job":    s.runner.State(),
		"files":  files,
		"ollama": s.ollama(cfg.LLM.Host),
		"config": map[string]any{"modes": cfg.LLM.Modes, "defaultMode": cfg.LLM.DefaultMode, "keepNames": cfg.LLM.KeepNames},
	})
}

func (s *server) handleEvents(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "Потоковая передача недоступна")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	backlog, lines, unsubscribe := s.hub.Subscribe()
	defer unsubscribe()
	send := func(line string) {
		data, _ := json.Marshal(line)
		fmt.Fprintf(w, "data: %s\n\n", data)
	}
	fmt.Fprint(w, "event: reset\ndata: {}\n\n")
	for _, line := range backlog {
		send(line)
	}
	flusher.Flush()
	ping := time.NewTicker(20 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-s.ctx.Done():
			return
		case line := <-lines:
			send(line)
			flusher.Flush()
		case <-ping.C:
			fmt.Fprint(w, ": ping\n\n")
			flusher.Flush()
		}
	}
}

// safeFileName оставляет только имя файла без путей и символов, запрещённых в Windows.
func safeFileName(name string) string {
	name = strings.ReplaceAll(name, "\\", "/")
	name = name[strings.LastIndex(name, "/")+1:]
	name = strings.Trim(badNameRe.ReplaceAllString(name, "_"), " .")
	return name
}

func (s *server) saveUpload(part io.Reader, name string, dir string) (string, error) {
	head := make([]byte, 1024)
	n, err := io.ReadFull(part, head)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return "", err
	}
	head = head[:n]
	if !bytes.Contains(head, []byte("%PDF-")) {
		return "", fmt.Errorf("%s: это не PDF", name)
	}
	tmp, err := os.CreateTemp(dir, ".upload-*.tmp")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	hash := sha256.New()
	_, err = io.Copy(io.MultiWriter(tmp, hash), io.MultiReader(bytes.NewReader(head), part))
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return "", err
	}
	sum := hash.Sum(nil)

	stem := strings.TrimSuffix(name, filepath.Ext(name))
	for i := 1; ; i++ {
		candidate := name
		if i > 1 {
			candidate = fmt.Sprintf("%s (%d).pdf", stem, i)
		}
		target := filepath.Join(dir, candidate)
		existing, err := fileSum(target)
		if errors.Is(err, os.ErrNotExist) {
			return target, os.Rename(tmp.Name(), target)
		}
		if err != nil {
			return "", err
		}
		if bytes.Equal(existing, sum) { // тот же файл уже лежит в очереди
			return target, nil
		}
	}
}

func fileSum(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, f); err != nil {
		return nil, err
	}
	return hash.Sum(nil), nil
}

func (s *server) handleUpload(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	dir := filepath.Join(s.root, cfg.Paths.InputDir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUpload)
	reader, err := r.MultipartReader()
	if err != nil {
		writeError(w, http.StatusBadRequest, "Ожидалась загрузка файлов")
		return
	}
	var saved []string
	var rejected []string
	for {
		part, err := reader.NextPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			writeError(w, http.StatusBadRequest, "Загрузка прервалась: "+err.Error())
			return
		}
		if part.FileName() == "" {
			continue
		}
		name := safeFileName(part.FileName())
		if !strings.EqualFold(filepath.Ext(name), ".pdf") || name == ".pdf" {
			rejected = append(rejected, part.FileName()+": нужен файл .pdf")
			continue
		}
		target, err := s.saveUpload(part, name, dir)
		if err != nil {
			rejected = append(rejected, err.Error())
			continue
		}
		saved = append(saved, target)
	}
	if len(saved) > 0 {
		if err := s.runner.Quick(s.ctx, append([]string{"add"}, saved...)...); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"added": len(saved), "rejected": rejected})
}

func (s *server) handleJob(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Action string `json:"action"`
		Hash   string `json:"hash"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	var err error
	switch req.Action {
	case "start":
		err = s.runner.Start(s.ctx, "Перевод очереди", []string{"add"}, []string{"resume"})
	case "translate", "rerender":
		r.SetPathValue("hash", req.Hash)
		file, cfg, ok := s.lookup(w, r)
		if !ok {
			return
		}
		path, pathErr := s.bookPath(cfg, file.Hash)
		if pathErr != nil {
			writeError(w, http.StatusInternalServerError, pathErr.Error())
			return
		}
		if req.Action == "translate" {
			err = s.runner.Start(s.ctx, "Перевод: "+file.Name, []string{"run", path})
		} else {
			err = s.runner.Start(s.ctx, "Сборка PDF: "+file.Name, []string{"rerender", path})
		}
	default:
		writeError(w, http.StatusBadRequest, "Неизвестное действие")
		return
	}
	if errors.Is(err, errJobRunning) {
		writeError(w, http.StatusConflict, "Дождитесь конца текущей задачи или остановите её")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.runner.State())
}

func (s *server) bookPath(cfg *Config, hash string) (string, error) {
	db, err := openDB(s.root, cfg)
	if err != nil {
		return "", err
	}
	defer db.Close()
	var path string
	err = db.QueryRow(`SELECT path FROM files WHERE file_hash = ?`, hash).Scan(&path)
	return path, err
}

func (s *server) handleStop(w http.ResponseWriter, r *http.Request) {
	if err := s.runner.Stop(); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.runner.State())
}

func (s *server) handleBlocks(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	db, err := openDB(s.root, cfg)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer db.Close()
	blocks, err := loadBlocks(db, s.root, cfg, file.Hash, cfg.ModeFor(file.Mode))
	if errors.Is(err, errNotExtracted) {
		writeJSON(w, http.StatusOK, map[string]any{"blocks": []Block{}})
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"blocks": blocks})
}

func (s *server) handlePDF(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	path := outputPath(s.root, cfg, file.Name)
	if _, err := os.Stat(path); err != nil {
		writeError(w, http.StatusNotFound, "PDF ещё не собран")
		return
	}
	w.Header().Set("Content-Type", "application/pdf")
	if r.URL.Query().Get("download") == "1" {
		w.Header().Set("Content-Disposition", downloadHeader(file.Name))
	}
	http.ServeFile(w, r, path)
}

// downloadHeader отличает перевод от исходника в папке загрузок и корректно передаёт кириллицу.
func downloadHeader(name string) string {
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	return mime.FormatMediaType("attachment", map[string]string{"filename": stem + " (перевод).pdf"})
}

func (s *server) handleMode(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var req struct {
		Mode string `json:"mode"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || !cfg.hasMode(req.Mode) {
		writeError(w, http.StatusBadRequest, "Неизвестный режим")
		return
	}
	// Python читает режим книги на каждой стадии: смена на ходу смешала бы переводы двух режимов.
	if s.runner.State().Running {
		writeError(w, http.StatusConflict, "Режим можно менять, когда перевод не идёт")
		return
	}
	if err := s.runner.Quick(s.ctx, "set-mode", file.Hash, req.Mode); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

func (s *server) handleAsset(w http.ResponseWriter, r *http.Request) {
	hash, name := r.PathValue("hash"), r.PathValue("name")
	if !hashRe.MatchString(hash) || !assetRe.MatchString(name) || strings.HasPrefix(name, ".") {
		writeError(w, http.StatusBadRequest, "Неверное имя файла")
		return
	}
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	http.ServeFile(w, r, filepath.Join(s.root, cfg.Paths.WorkDir, hash, "assets", name))
}

func (s *server) handleRemove(w http.ResponseWriter, r *http.Request) {
	file, _, ok := s.lookup(w, r)
	if !ok {
		return
	}
	if s.runner.State().Running {
		writeError(w, http.StatusConflict, "Дождитесь конца текущей задачи или остановите её")
		return
	}
	if err := s.runner.Quick(s.ctx, "remove", file.Hash); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

func (s *server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	var req struct {
		Modes       []Mode `json:"modes"`
		DefaultMode string `json:"defaultMode"`
		KeepNames   *bool  `json:"keepNames"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	if !cfg.hasMode(req.DefaultMode) {
		writeError(w, http.StatusBadRequest, "Неизвестный режим")
		return
	}
	for _, m := range req.Modes {
		if !cfg.hasMode(m.ID) {
			writeError(w, http.StatusBadRequest, "Неизвестный режим")
			return
		}
		if !modelNameRe.MatchString(m.Pass1) || !modelNameRe.MatchString(m.Pass2) {
			writeError(w, http.StatusBadRequest, "Неверное имя модели")
			return
		}
	}
	if s.runner.State().Running {
		writeError(w, http.StatusConflict, "Модели можно менять, когда перевод не идёт")
		return
	}
	if err := saveModes(s.root, req.Modes, req.DefaultMode); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if req.KeepNames != nil {
		if err := saveKeepNames(s.root, *req.KeepNames); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

// guard защищает локальный сервер от запросов чужих сайтов: сверяет Host (DNS rebinding)
// и Origin изменяющих запросов (CSRF). Без него любая открытая вкладка могла бы управлять очередью.
func guard(addr string, next http.Handler) http.Handler {
	_, port, _ := net.SplitHostPort(addr)
	allowed := map[string]bool{"localhost:" + port: true, "127.0.0.1:" + port: true, "[::1]:" + port: true}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !allowed[r.Host] {
			http.Error(w, "forbidden host", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			origin := r.Header.Get("Origin")
			if origin != "" && origin != "http://"+r.Host {
				http.Error(w, "forbidden origin", http.StatusForbidden)
				return
			}
		}
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		if !strings.HasPrefix(r.URL.Path, "/api/files/") || !strings.HasSuffix(r.URL.Path, "/pdf") {
			h.Set("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
		}
		next.ServeHTTP(w, r)
	})
}

// Место чтения лежит в папке книги: так оно не зависит от браузера и удаляется вместе с книгой.
func positionPath(root string, cfg *Config, hash string) string {
	return filepath.Join(root, cfg.Paths.WorkDir, hash, "position.json")
}

func (s *server) handleGetPosition(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var position struct {
		Index *int `json:"index"`
	}
	if raw, err := os.ReadFile(positionPath(s.root, cfg, file.Hash)); err == nil {
		// Повреждённый файл равносилен его отсутствию: книга просто откроется с начала.
		_ = json.Unmarshal(raw, &position)
	}
	writeJSON(w, http.StatusOK, position)
}

func (s *server) handlePutPosition(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var position struct {
		Index *int `json:"index"`
	}
	err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024)).Decode(&position)
	if err != nil || position.Index == nil || *position.Index < 0 {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	path := positionPath(s.root, cfg, file.Hash)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	raw, _ := json.Marshal(position)
	if err := writeAtomic(path, raw); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/state", s.handleState)
	mux.HandleFunc("GET /api/events", s.handleEvents)
	mux.HandleFunc("POST /api/upload", s.handleUpload)
	mux.HandleFunc("POST /api/jobs", s.handleJob)
	mux.HandleFunc("POST /api/jobs/stop", s.handleStop)
	mux.HandleFunc("GET /api/files/{hash}/blocks", s.handleBlocks)
	mux.HandleFunc("PUT /api/files/{hash}/mode", s.handleMode)
	mux.HandleFunc("GET /api/files/{hash}/pdf", s.handlePDF)
	mux.HandleFunc("GET /api/files/{hash}/assets/{name}", s.handleAsset)
	mux.HandleFunc("DELETE /api/files/{hash}", s.handleRemove)
	mux.HandleFunc("GET /api/files/{hash}/position", s.handleGetPosition)
	mux.HandleFunc("PUT /api/files/{hash}/position", s.handlePutPosition)
	mux.HandleFunc("GET /api/files/{hash}/notes", s.handleGetNotes)
	mux.HandleFunc("PUT /api/files/{hash}/notes/{index}", s.handlePutNote)
	mux.HandleFunc("DELETE /api/files/{hash}/notes/{index}", s.handleDeleteNote)
	mux.HandleFunc("GET /api/groups", s.handleGetGroups)
	mux.HandleFunc("POST /api/groups", s.handleCreateGroup)
	mux.HandleFunc("PUT /api/groups/{id}", s.handleRenameGroup)
	mux.HandleFunc("DELETE /api/groups/{id}", s.handleDeleteGroup)
	mux.HandleFunc("PUT /api/files/{hash}/group", s.handleSetBookGroup)
	mux.HandleFunc("GET /api/glossary", s.handleGetGlossary)
	mux.HandleFunc("PUT /api/glossary", s.handlePutGlossary)
	mux.HandleFunc("POST /api/glossary/terms", s.handleAddTerms)
	mux.HandleFunc("POST /api/glossary/import", s.handleImport)
	mux.HandleFunc("PUT /api/glossary/plain-names", s.handlePutPlainNames)
	mux.HandleFunc("PUT /api/glossary/characters", s.handlePutCharacter)
	mux.HandleFunc("GET /api/files/{hash}/candidates", s.handleCandidates)
	mux.HandleFunc("PUT /api/config", s.handlePutConfig)

	dist, err := fs.Sub(distFS, "dist")
	if err != nil {
		log.Fatal(err)
	}
	mux.Handle("/", http.FileServerFS(dist))
	return mux
}

func openBrowser(url string) {
	cmd := exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	if err := cmd.Start(); err != nil {
		log.Printf("Откройте в браузере: %s", url)
	}
}

func main() {
	rootFlag := flag.String("root", ".", "папка проекта с config.yaml")
	addr := flag.String("addr", "127.0.0.1:8765", "адрес интерфейса")
	noOpen := flag.Bool("no-open", false, "не открывать браузер")
	flag.Parse()

	root, err := filepath.Abs(*rootFlag)
	if err != nil {
		log.Fatal(err)
	}
	if _, err := loadConfig(root); err != nil {
		log.Fatalf("Не найден config.yaml в %s: %v", root, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	hub := newLogHub()
	s := &server{
		root:   root,
		ctx:    ctx,
		hub:    hub,
		runner: NewRunner(root, findPython(root), hub),
		client: &http.Client{Timeout: 2 * time.Second},
	}
	// Файлы, положенные в in/ вручную, сразу видны в очереди.
	if err := s.runner.Quick(ctx, "add"); err != nil {
		log.Printf("Не удалось просмотреть папку очереди: %v", err)
	}

	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatal(err)
	}
	httpServer := &http.Server{Handler: guard(*addr, s.routes()), ReadHeaderTimeout: 10 * time.Second}
	url := "http://" + *addr
	log.Printf("Интерфейс: %s (Ctrl+C для выхода)", url)
	if !*noOpen {
		openBrowser(url)
	}
	go func() {
		<-ctx.Done()
		s.runner.Stop()
		httpServer.Close()
	}()
	if err := httpServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
