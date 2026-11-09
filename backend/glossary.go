package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// Term — строка глоссария.
type Term struct {
	Source string `json:"source"`
	Target string `json:"target"`
}

var termCleaner = strings.NewReplacer("\t", " ", "\r", " ", "\n", " ")

func cleanTerm(t Term) Term {
	return Term{strings.TrimSpace(termCleaner.Replace(t.Source)), strings.TrimSpace(termCleaner.Replace(t.Target))}
}

func parseGlossary(raw []byte) (terms []Term, comments []string) {
	terms = []Term{}
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r")
		if strings.HasPrefix(line, "#") {
			comments = append(comments, line)
			continue
		}
		if cols := strings.Split(line, "\t"); len(cols) >= 2 {
			terms = append(terms, cleanTerm(Term{cols[0], cols[1]}))
		}
	}
	return terms, comments
}

func formatGlossary(terms []Term, comments []string) []byte {
	var out bytes.Buffer
	for _, comment := range comments {
		if strings.HasPrefix(comment, "#") && !strings.ContainsAny(comment, "\r\n") {
			out.WriteString(comment + "\n")
		}
	}
	for _, t := range terms {
		if t = cleanTerm(t); t.Source != "" && t.Target != "" {
			out.WriteString(t.Source + "\t" + t.Target + "\n")
		}
	}
	return out.Bytes()
}

// mergeTerms добавляет новые термины в конец; у существующего заменяет перевод.
func mergeTerms(terms, incoming []Term) (merged []Term, added, updated int) {
	merged = append([]Term(nil), terms...)
	index := map[string]int{}
	for i, t := range merged {
		index[t.Source] = i
	}
	for _, t := range incoming {
		if t = cleanTerm(t); t.Source == "" || t.Target == "" {
			continue
		}
		if i, ok := index[t.Source]; ok {
			if merged[i].Target != t.Target {
				merged[i].Target = t.Target
				updated++
			}
			continue
		}
		index[t.Source] = len(merged)
		merged = append(merged, t)
		added++
	}
	return merged, added, updated
}

func readGlossaryFile(path string) ([]Term, []string, error) {
	raw, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, nil, err
	}
	terms, comments := parseGlossary(raw)
	return terms, comments, nil
}

func (s *server) handleGetGlossary(w http.ResponseWriter, r *http.Request) {
	group, cfg, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	files := contextFor(s.root, cfg, group)
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	terms, comments, err := readGlossaryFile(files.Glossary)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	hints := 0
	if raw, err := os.ReadFile(files.Hints); err == nil {
		list, _ := parseGlossary(raw)
		hints = len(list)
	}
	writeJSON(w, http.StatusOK, map[string]any{"terms": terms, "comments": comments, "hints": hints, "hintsFile": relativePath(s.root, files.Hints)})
}

// handleImport передаёт .xlsx в Python: разбор таблицы и правила сортировки живут в src/glossary_import.py.
func (s *server) handleImport(w http.ResponseWriter, r *http.Request) {
	group, _, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 32<<20)
	file, _, err := r.FormFile("file")
	if err != nil {
		writeError(w, http.StatusBadRequest, "Ожидался файл .xlsx размером до 32 МБ")
		return
	}
	defer file.Close()
	tmp, err := os.CreateTemp("", "glossary-*.xlsx")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer os.Remove(tmp.Name())
	_, err = io.Copy(tmp, file)
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	out, err := s.runner.Output(s.ctx, "import-glossary", tmp.Name(), "--group", group)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Не удалось прочитать таблицу. Нужен .xlsx с колонками оригинала и перевода.")
		return
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	io.WriteString(w, lines[len(lines)-1])
}

func (s *server) handlePutGlossary(w http.ResponseWriter, r *http.Request) {
	group, cfg, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	path := contextFor(s.root, cfg, group).Glossary
	var req struct {
		Terms    []Term   `json:"terms"`
		Comments []string `json:"comments"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	if err := writeAtomic(path, formatGlossary(req.Terms, req.Comments)); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

func (s *server) handleAddTerms(w http.ResponseWriter, r *http.Request) {
	group, cfg, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	path := contextFor(s.root, cfg, group).Glossary
	var req struct {
		Terms []Term `json:"terms"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	terms, comments, err := readGlossaryFile(path)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	merged, added, updated := mergeTerms(terms, req.Terms)
	if added+updated > 0 {
		if err := writeAtomic(path, formatGlossary(merged, comments)); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]int{"added": added, "updated": updated})
}

// handleCandidates отдаёт имена книги. Правила поиска живут в src/names.py: по ним же конвейер решает,
// что оставить по-английски, поэтому список на экране совпадает с тем, что произойдёт при переводе.
func (s *server) handleCandidates(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	_, err := os.Stat(filepath.Join(s.root, cfg.Paths.WorkDir, file.Hash, "blocks.json"))
	if errors.Is(err, os.ErrNotExist) {
		// Новую книгу разбираем без перевода: глоссарий полезнее всего до него.
		if s.runner.State().Running {
			writeError(w, http.StatusConflict, "Книгу можно разобрать, когда перевод не идёт")
			return
		}
		if err := s.runner.Quick(s.ctx, "extract", file.Hash); err != nil {
			writeError(w, http.StatusInternalServerError, "Не удалось разобрать книгу: "+err.Error())
			return
		}
	}
	out, err := s.runner.Output(s.ctx, "names", file.Hash)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	io.WriteString(w, lines[len(lines)-1])
}

// handlePutPlainNames сохраняет слова, которые нужно переводить обычным образом, а не оставлять по-английски.
func (s *server) handlePutPlainNames(w http.ResponseWriter, r *http.Request) {
	group, cfg, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	var req struct {
		Names []string `json:"names"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	var out bytes.Buffer
	for _, name := range req.Names {
		if name = strings.TrimSpace(termCleaner.Replace(name)); name != "" {
			out.WriteString(name + "\n")
		}
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	if err := writeAtomic(contextFor(s.root, cfg, group).Plain, out.Bytes()); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

// setCharacter меняет решение о персонаже в characters.tsv. Пустое значение возвращает вывод по тексту.
func setCharacter(raw []byte, name, value string) []byte {
	var out bytes.Buffer
	found := false
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r")
		if cols := strings.Split(line, "\t"); !strings.HasPrefix(line, "#") && cols[0] == name {
			found = true
			if value != "" {
				out.WriteString(name + "\t" + value + "\n")
			}
			continue
		}
		out.WriteString(line + "\n")
	}
	if !found && value != "" {
		out.WriteString(name + "\t" + value + "\n")
	}
	return out.Bytes()
}

// handlePutCharacter сохраняет пол персонажей: m, f, x (не персонаж) или пусто. Список пишется целиком или никак.
func (s *server) handlePutCharacter(w http.ResponseWriter, r *http.Request) {
	group, cfg, ok := s.requestGroup(w, r)
	if !ok {
		return
	}
	var req struct {
		Items []struct {
			Term  string `json:"term"`
			Value string `json:"value"`
		} `json:"items"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	for i, item := range req.Items {
		name := strings.TrimSpace(termCleaner.Replace(item.Term))
		if name == "" || strings.HasPrefix(name, "#") || !(item.Value == "" || item.Value == "m" || item.Value == "f" || item.Value == "x") {
			writeError(w, http.StatusBadRequest, "Неверное имя или значение")
			return
		}
		req.Items[i].Term = name
	}
	path := contextFor(s.root, cfg, group).Characters
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	raw, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, item := range req.Items {
		raw = setCharacter(raw, item.Term, item.Value)
	}
	if err := writeAtomic(path, raw); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{})
}

func relativePath(root, path string) string {
	if rel, err := filepath.Rel(root, path); err == nil {
		return rel
	}
	return path
}
