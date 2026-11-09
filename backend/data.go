package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
	_ "modernc.org/sqlite"
)

// Config — часть config.yaml, нужная интерфейсу.
type Config struct {
	Paths struct {
		InputDir   string `yaml:"input_dir"`
		Output     string `yaml:"output_dir"`
		WorkDir    string `yaml:"work_dir"`
		DB         string `yaml:"db"`
		Glossary   string `yaml:"glossary"`
		Hints      string `yaml:"hints"`
		PlainNames string `yaml:"plain_names"`
		Characters string `yaml:"characters"`
	} `yaml:"paths"`
	LLM struct {
		Host        string `yaml:"host"`
		PromptVer   int    `yaml:"prompt_ver"`
		DefaultMode string `yaml:"default_mode"`
		KeepNames   bool   `yaml:"keep_names"`
		Modes       []Mode `yaml:"modes"`
	} `yaml:"llm"`
}

// Mode — вариант обработки книги.
type Mode struct {
	ID          string `yaml:"id" json:"id"`
	Label       string `yaml:"label" json:"label"`
	Description string `yaml:"description" json:"description"`
	Pass1       string `yaml:"pass1_model" json:"pass1"`
	Pass2       string `yaml:"pass2_model" json:"pass2"`
}

// ModeFor повторяет Pipeline.mode_id: неизвестный режим заменяется режимом по умолчанию.
func (c *Config) ModeFor(id string) Mode {
	for _, m := range c.LLM.Modes {
		if m.ID == id {
			return m
		}
	}
	for _, m := range c.LLM.Modes {
		if m.ID == c.LLM.DefaultMode {
			return m
		}
	}
	return Mode{}
}

func (c *Config) hasMode(id string) bool {
	for _, m := range c.LLM.Modes {
		if m.ID == id {
			return true
		}
	}
	return false
}

func loadConfig(root string) (*Config, error) {
	raw, err := os.ReadFile(filepath.Join(root, "config.yaml"))
	if err != nil {
		return nil, err
	}
	var cfg Config
	if err := yaml.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("config.yaml: %w", err)
	}
	return &cfg, nil
}

var modeIDRe = regexp.MustCompile(`^\s*-\s*id:\s*(\S+)`)

// saveModes меняет только значения: комментарии и описания в config.yaml остаются на месте.
var keepNamesRe = regexp.MustCompile(`(?m)^(\s*keep_names:\s*)(true|false)`)

// saveKeepNames меняет только значение настройки: комментарии в config.yaml остаются.
func saveKeepNames(root string, keep bool) error {
	path := filepath.Join(root, "config.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if !keepNamesRe.Match(raw) {
		return errors.New("в config.yaml нет настройки keep_names")
	}
	return writeAtomic(path, keepNamesRe.ReplaceAll(raw, []byte("${1}"+strconv.FormatBool(keep))))
}

func saveModes(root string, updates []Mode, defaultMode string) error {
	path := filepath.Join(root, "config.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	byID := map[string]Mode{}
	for _, u := range updates {
		byID[u.ID] = u
	}
	pass1Re := regexp.MustCompile(`^(\s*pass1_model:[ \t]*)\S+`)
	pass2Re := regexp.MustCompile(`^(\s*pass2_model:[ \t]*)\S+`)
	defaultRe := regexp.MustCompile(`^(\s*default_mode:[ \t]*)\S+`)
	lines := strings.Split(string(raw), "\n")
	current, replaced, defaults := "", 0, 0
	for i, line := range lines {
		if m := modeIDRe.FindStringSubmatch(line); m != nil {
			current = m[1]
		}
		if defaultRe.MatchString(line) {
			lines[i] = defaultRe.ReplaceAllString(line, "${1}"+defaultMode)
			defaults++
		}
		if u, ok := byID[current]; ok {
			if pass1Re.MatchString(line) {
				lines[i] = pass1Re.ReplaceAllString(line, "${1}"+u.Pass1)
				replaced++
			} else if pass2Re.MatchString(line) {
				lines[i] = pass2Re.ReplaceAllString(line, "${1}"+u.Pass2)
				replaced++
			}
		}
	}
	if defaults != 1 || replaced != 2*len(updates) {
		return errors.New("config.yaml изменён вручную: не удалось найти режимы для записи")
	}
	return writeAtomic(path, []byte(strings.Join(lines, "\n")))
}

func writeAtomic(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// openDB открывает базу только на чтение: пишет в неё исключительно Python.
func openDB(root string, cfg *Config) (*sql.DB, error) {
	path, err := filepath.Abs(filepath.Join(root, cfg.Paths.DB))
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(path); err != nil {
		return nil, err
	}
	u := url.URL{Scheme: "file", Path: "/" + strings.TrimPrefix(filepath.ToSlash(path), "/")}
	return sql.Open("sqlite", u.String()+"?mode=ro&_pragma=busy_timeout(5000)")
}

// chunkRow — одна попытка перевода куска текста.
type chunkRow struct {
	Block, Part int
	Model       string
	Status      string
	Flagged     bool
	Reasons     string
	Seconds     float64
	Src, Dst    string
}

type chunkKey struct{ Block, Part int }

// Состояния куска на ленте книги.
const (
	statePending    = "p" // ждёт перевода
	stateError      = "e" // перевод не удался
	stateFirst      = "a" // переведён первым проходом, замечаний нет
	stateFlagged    = "f" // помечен, ждёт второго прохода
	stateSecond     = "b" // исправлен вторым проходом
	stateStillFlagd = "x" // замечания остались и после второго прохода
)

func reasonCount(r *chunkRow) int {
	if r.Reasons == "" {
		return 0
	}
	return len(strings.Split(r.Reasons, "; "))
}

// classify повторяет правило Pipeline.best_translation из src/cli.py: вторая модель заменяет первую,
// только если первая помечена, а у второй замечаний не больше. Возвращает состояние и итоговую строку.
func classify(first, second *chunkRow) (string, *chunkRow) {
	switch first.Status {
	case "pending":
		return statePending, nil
	case "error":
		return stateError, nil
	}
	if !first.Flagged {
		return stateFirst, first
	}
	if second == nil || second.Status == "pending" {
		return stateFlagged, first
	}
	if second.Status == "error" {
		return stateError, first
	}
	if reasonCount(second) <= reasonCount(first) {
		if second.Flagged {
			return stateStillFlagd, second
		}
		return stateSecond, second
	}
	return stateStillFlagd, first
}

type chunkPair struct {
	Key           chunkKey
	First, Second *chunkRow
}

func loadChunks(db *sql.DB, hash string, cfg *Config, mode Mode, withText bool) ([]chunkPair, error) {
	text := "'', ''"
	if withText {
		text = "src, COALESCE(dst, '')"
	}
	rows, err := db.Query(`SELECT block_id, part, model, status, flagged, COALESCE(reasons, ''), COALESCE(seconds, 0), `+text+`
		FROM chunks WHERE file_hash = ? AND prompt_ver = ? AND model IN (?, ?)`,
		hash, cfg.LLM.PromptVer, mode.Pass1, mode.Pass2)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	pairs := map[chunkKey]*chunkPair{}
	for rows.Next() {
		var r chunkRow
		if err := rows.Scan(&r.Block, &r.Part, &r.Model, &r.Status, &r.Flagged, &r.Reasons, &r.Seconds, &r.Src, &r.Dst); err != nil {
			return nil, err
		}
		key := chunkKey{r.Block, r.Part}
		p := pairs[key]
		if p == nil {
			p = &chunkPair{Key: key}
			pairs[key] = p
		}
		row := r
		if r.Model == mode.Pass1 {
			p.First = &row
		}
		if r.Model == mode.Pass2 {
			p.Second = &row
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]chunkPair, 0, len(pairs))
	for _, p := range pairs {
		if p.First != nil { // строка только второй модели значит, что первую сменили в настройках
			out = append(out, *p)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i].Key, out[j].Key
		return a.Block < b.Block || (a.Block == b.Block && a.Part < b.Part)
	})
	return out, nil
}

// FileInfo — книга в очереди.
type FileInfo struct {
	Hash   string `json:"hash"`
	Name   string `json:"name"`
	Path   string `json:"-"`
	Status string `json:"status"`
	Mode   string `json:"mode"`
	// RenderedMode — режим, которым собран текущий PDF: он может отличаться от выбранного.
	RenderedMode string  `json:"renderedMode"`
	Strip        string  `json:"strip"`
	Total        int     `json:"total"`
	Done         int     `json:"done"`
	Flagged      int     `json:"flagged"`
	Second       int     `json:"second"`
	Errors       int     `json:"errors"`
	Remaining    float64 `json:"remainingSeconds"`
	HasOutput    bool    `json:"hasOutput"`
	Stale        bool    `json:"stale"`
	OutputMtime  int64   `json:"outputMtime"`
}

func outputPath(root string, cfg *Config, name string) string {
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	return filepath.Join(root, cfg.Paths.Output, stem+".pdf")
}

func listFiles(db *sql.DB, root string, cfg *Config) ([]FileInfo, error) {
	rows, err := db.Query(`SELECT file_hash, name, path, status, COALESCE(mode, ''), COALESCE(rendered_mode, '') FROM files ORDER BY rowid`)
	if err != nil {
		return nil, err
	}
	files := []FileInfo{}
	for rows.Next() {
		var f FileInfo
		if err := rows.Scan(&f.Hash, &f.Name, &f.Path, &f.Status, &f.Mode, &f.RenderedMode); err != nil {
			rows.Close()
			return nil, err
		}
		files = append(files, f)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}

	for i := range files {
		f := &files[i]
		mode := cfg.ModeFor(f.Mode)
		f.Mode = mode.ID
		chunks, err := loadChunks(db, f.Hash, cfg, mode, false)
		if err != nil {
			return nil, err
		}
		summarize(f, chunks)
		if st, err := os.Stat(outputPath(root, cfg, f.Name)); err == nil {
			f.HasOutput = true
			f.OutputMtime = st.ModTime().Unix()
		}
		if f.Stale, err = isStale(db, f, len(chunks)); err != nil {
			return nil, err
		}
	}
	return files, nil
}

// isStale повторяет Pipeline.needs_work для начатых книг: PDF собран другим режимом либо кусков в базе
// больше, чем строк под текущие модели и промпт. У книги из одних картинок кусков нет, и это не устаревание.
func isStale(db *sql.DB, f *FileInfo, current int) (bool, error) {
	if f.Status == "new" {
		return false, nil
	}
	if f.Status == "rendered" && f.RenderedMode != f.Mode {
		return true, nil
	}
	var known int
	err := db.QueryRow(`SELECT COUNT(*) FROM (SELECT DISTINCT block_id, part FROM chunks WHERE file_hash = ?)`, f.Hash).Scan(&known)
	return current < known, err
}

func summarize(f *FileInfo, chunks []chunkPair) {
	var strip strings.Builder
	var sum1, sum2 float64
	var done1, done2, wait1, wait2 int
	for _, c := range chunks {
		state, _ := classify(c.First, c.Second)
		strip.WriteString(state)
		f.Total++
		switch state {
		case stateFirst:
			f.Done++
		case stateSecond:
			f.Done++
			f.Second++
		case stateStillFlagd:
			f.Done++
			f.Flagged++
		case stateFlagged:
			f.Flagged++
		case stateError:
			f.Errors++
		}
		if c.First.Status == "done" {
			done1++
			sum1 += c.First.Seconds
		} else {
			wait1++
		}
		if c.Second != nil && c.Second != c.First {
			if c.Second.Status == "done" {
				done2++
				sum2 += c.Second.Seconds
			} else {
				wait2++
			}
		}
	}
	f.Strip = strip.String()
	if done1 > 0 {
		f.Remaining += float64(wait1) * sum1 / float64(done1)
	}
	if done2 > 0 {
		f.Remaining += float64(wait2) * sum2 / float64(done2)
	} else if done1 > 0 {
		// Скорость второй модели ещё неизвестна; на RTX 4050 12B примерно в 4,5 раза медленнее 4B.
		f.Remaining += float64(wait2) * 4.5 * sum1 / float64(done1)
	}
}

// BlockPart — кусок текста с итоговым переводом и альтернативой.
type BlockPart struct {
	Index    int    `json:"index"` // позиция на ленте книги
	Src      string `json:"src"`
	Dst      string `json:"dst"`
	Model    string `json:"model"`
	State    string `json:"state"`
	Reasons  string `json:"reasons,omitempty"`
	Alt      string `json:"alt,omitempty"`
	AltModel string `json:"altModel,omitempty"`
}

// Block — блок из blocks.json вместе с переводом.
type Block struct {
	ID           int         `json:"id"`
	Type         string      `json:"type"`
	Page         int         `json:"page"`
	Text         string      `json:"text,omitempty"`
	Level        int         `json:"level,omitempty"`
	SectionBreak bool        `json:"sectionBreak,omitempty"`
	Asset        string      `json:"asset,omitempty"`
	Parts        []BlockPart `json:"parts,omitempty"`
}

var errNotExtracted = errors.New("книга ещё не разобрана")

func loadBlocks(db *sql.DB, root string, cfg *Config, hash string, mode Mode) ([]Block, error) {
	raw, err := os.ReadFile(filepath.Join(root, cfg.Paths.WorkDir, hash, "blocks.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, errNotExtracted
	}
	if err != nil {
		return nil, err
	}
	var doc struct {
		Blocks []struct {
			ID           int    `json:"id"`
			Type         string `json:"type"`
			Page         int    `json:"page"`
			Text         string `json:"text"`
			Level        int    `json:"level"`
			SectionBreak bool   `json:"section_break"`
			Asset        string `json:"asset"`
		} `json:"blocks"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("blocks.json: %w", err)
	}
	chunks, err := loadChunks(db, hash, cfg, mode, true)
	if err != nil {
		return nil, err
	}
	parts := map[int][]BlockPart{}
	for i, c := range chunks {
		state, chosen := classify(c.First, c.Second)
		part := BlockPart{Index: i, Src: c.First.Src, State: state}
		if chosen != nil {
			part.Dst, part.Model, part.Reasons = chosen.Dst, chosen.Model, chosen.Reasons
			if other := c.Second; chosen == c.Second {
				other = c.First
				if other != chosen {
					part.Alt, part.AltModel = other.Dst, other.Model
				}
			} else if other != nil && other.Status == "done" {
				part.Alt, part.AltModel = other.Dst, other.Model
			}
		} else if state == stateError {
			part.Reasons = c.First.Reasons
		}
		parts[c.Key.Block] = append(parts[c.Key.Block], part)
	}
	out := make([]Block, 0, len(doc.Blocks))
	for _, b := range doc.Blocks {
		out = append(out, Block{ID: b.ID, Type: b.Type, Page: b.Page, Text: b.Text, Level: b.Level,
			SectionBreak: b.SectionBreak, Asset: b.Asset, Parts: parts[b.ID]})
	}
	return out, nil
}
