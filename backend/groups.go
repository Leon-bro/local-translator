package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Контекст перевода (глоссарий, подсказки, пол персонажей, обычные слова) у каждой группы книг свой.
// Группа по умолчанию использует файлы из config.yaml, остальные лежат в context/<id>/.
const (
	defaultGroup     = "default"
	defaultGroupName = "Без группы"
	groupsFileName   = "groups.json"
	contextDirName   = "context"
)

var groupIDRe = regexp.MustCompile(`^[a-z0-9-]{1,40}$`)

type Group struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type groupsData struct {
	Groups []Group           `json:"groups"`
	Books  map[string]string `json:"books"`
}

// readGroups возвращает ошибку на повреждённом файле: запись поверх стёрла бы все группы.
func readGroups(root string) (*groupsData, error) {
	data := &groupsData{Groups: []Group{}, Books: map[string]string{}}
	raw, err := os.ReadFile(filepath.Join(root, groupsFileName))
	if errors.Is(err, os.ErrNotExist) {
		return data, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, data); err != nil {
		return nil, fmt.Errorf("%s повреждён: %w", groupsFileName, err)
	}
	if data.Books == nil {
		data.Books = map[string]string{}
	}
	return data, nil
}

func writeGroups(root string, data *groupsData) error {
	raw, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	return writeAtomic(filepath.Join(root, groupsFileName), raw)
}

func (g *groupsData) has(id string) bool {
	if id == defaultGroup {
		return true
	}
	for _, group := range g.Groups {
		if group.ID == id {
			return true
		}
	}
	return false
}

func (g *groupsData) of(hash string) string {
	if id, ok := g.Books[hash]; ok && g.has(id) {
		return id
	}
	return defaultGroup
}

func (g *groupsData) nameTaken(name, except string) bool {
	if strings.EqualFold(name, defaultGroupName) {
		return true
	}
	for _, group := range g.Groups {
		if group.ID != except && strings.EqualFold(group.Name, name) {
			return true
		}
	}
	return false
}

type contextPaths struct {
	Glossary, Hints, Plain, Characters string
}

func contextFor(root string, cfg *Config, group string) contextPaths {
	characters := cfg.Paths.Characters
	if characters == "" {
		characters = "characters.tsv"
	}
	if group == defaultGroup {
		return contextPaths{
			filepath.Join(root, cfg.Paths.Glossary), filepath.Join(root, cfg.Paths.Hints),
			filepath.Join(root, cfg.Paths.PlainNames), filepath.Join(root, characters),
		}
	}
	dir := filepath.Join(root, contextDirName, group)
	return contextPaths{
		filepath.Join(dir, filepath.Base(cfg.Paths.Glossary)), filepath.Join(dir, filepath.Base(cfg.Paths.Hints)),
		filepath.Join(dir, filepath.Base(cfg.Paths.PlainNames)), filepath.Join(dir, filepath.Base(characters)),
	}
}

// requestGroup определяет группу запроса: ?group=id или ?book=hash, по умолчанию группа без названия.
// Термин из читалки идёт по книге: группа книги могла измениться, пока страница открыта.
func (s *server) requestGroup(w http.ResponseWriter, r *http.Request) (string, *Config, bool) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return "", nil, false
	}
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return "", nil, false
	}
	group := r.URL.Query().Get("group")
	if book := r.URL.Query().Get("book"); book != "" {
		group = data.of(book)
	}
	if group == "" {
		group = defaultGroup
	}
	if !data.has(group) {
		writeError(w, http.StatusBadRequest, "Такой группы нет")
		return "", nil, false
	}
	return group, cfg, true
}

type groupView struct {
	ID    string   `json:"id"`
	Name  string   `json:"name"`
	Books []string `json:"books"`
	Terms int      `json:"terms"`
}

func (s *server) groupViews(cfg *Config, data *groupsData) ([]groupView, error) {
	views := []groupView{{ID: defaultGroup, Name: defaultGroupName, Books: []string{}}}
	for _, group := range data.Groups {
		views = append(views, groupView{ID: group.ID, Name: group.Name, Books: []string{}})
	}
	at := map[string]int{}
	for i, view := range views {
		at[view.ID] = i
	}
	if db, err := openDB(s.root, cfg); err == nil {
		defer db.Close()
		files, err := listFiles(db, s.root, cfg)
		if err != nil {
			return nil, err
		}
		for _, file := range files {
			i := at[data.of(file.Hash)]
			views[i].Books = append(views[i].Books, file.Hash)
		}
	}
	for i := range views {
		raw, err := os.ReadFile(contextFor(s.root, cfg, views[i].ID).Glossary)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		terms, _ := parseGlossary(raw)
		views[i].Terms = len(terms)
	}
	return views, nil
}

func (s *server) writeGroupViews(w http.ResponseWriter, cfg *Config, data *groupsData) {
	views, err := s.groupViews(cfg, data)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"groups": views})
}

func (s *server) handleGetGroups(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeGroupViews(w, cfg, data)
}

func copyContext(from, to contextPaths) error {
	pairs := [][2]string{{from.Glossary, to.Glossary}, {from.Hints, to.Hints}, {from.Plain, to.Plain}, {from.Characters, to.Characters}}
	for _, pair := range pairs {
		raw, err := os.ReadFile(pair[0])
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if err := writeAtomic(pair[1], raw); err != nil {
			return err
		}
	}
	return nil
}

func newGroupID() (string, error) {
	raw := make([]byte, 4)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return "g" + hex.EncodeToString(raw), nil
}

func cleanGroupName(name string) string {
	return strings.TrimSpace(termCleaner.Replace(name))
}

// handleCreateGroup создаёт группу: пустую или с копией контекста другой группы.
func (s *server) handleCreateGroup(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	var req struct {
		Name     string `json:"name"`
		CopyFrom string `json:"copyFrom"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	name := cleanGroupName(req.Name)
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if name == "" || len([]rune(name)) > 80 {
		writeError(w, http.StatusBadRequest, "Укажите название группы, до 80 символов")
		return
	}
	if data.nameTaken(name, "") {
		writeError(w, http.StatusConflict, "Группа с таким названием уже есть")
		return
	}
	if req.CopyFrom != "" && !data.has(req.CopyFrom) {
		writeError(w, http.StatusBadRequest, "Группы, из которой нужно копировать, нет")
		return
	}
	id, err := newGroupID()
	if err == nil {
		err = os.MkdirAll(filepath.Join(s.root, contextDirName, id), 0o755)
	}
	if err == nil && req.CopyFrom != "" {
		err = copyContext(contextFor(s.root, cfg, req.CopyFrom), contextFor(s.root, cfg, id))
	}
	if err == nil {
		data.Groups = append(data.Groups, Group{ID: id, Name: name})
		err = writeGroups(s.root, data)
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeGroupViews(w, cfg, data)
}

func (s *server) handleRenameGroup(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	var req struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	id, name := r.PathValue("id"), cleanGroupName(req.Name)
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if id == defaultGroup || !data.has(id) {
		writeError(w, http.StatusBadRequest, "Эту группу нельзя переименовать")
		return
	}
	if name == "" || len([]rune(name)) > 80 {
		writeError(w, http.StatusBadRequest, "Укажите название группы, до 80 символов")
		return
	}
	if data.nameTaken(name, id) {
		writeError(w, http.StatusConflict, "Группа с таким названием уже есть")
		return
	}
	for i := range data.Groups {
		if data.Groups[i].ID == id {
			data.Groups[i].Name = name
		}
	}
	if err := writeGroups(s.root, data); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeGroupViews(w, cfg, data)
}

// handleDeleteGroup убирает пустую группу. Её файлы переезжают в context/_deleted: глоссарий долго собирать.
func (s *server) handleDeleteGroup(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	id := r.PathValue("id")
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if id == defaultGroup || !groupIDRe.MatchString(id) || !data.has(id) {
		writeError(w, http.StatusBadRequest, "Эту группу нельзя удалить")
		return
	}
	views, err := s.groupViews(cfg, data)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, view := range views {
		if view.ID == id && len(view.Books) > 0 {
			writeError(w, http.StatusConflict, "В группе есть книги. Сначала перенесите их в другую группу")
			return
		}
	}
	trash := filepath.Join(s.root, contextDirName, "_deleted")
	if err := os.MkdirAll(trash, 0o755); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	err = os.Rename(filepath.Join(s.root, contextDirName, id), filepath.Join(trash, id))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	kept := []Group{}
	for _, group := range data.Groups {
		if group.ID != id {
			kept = append(kept, group)
		}
	}
	data.Groups = kept
	for hash, group := range data.Books {
		if group == id {
			delete(data.Books, hash) // запись об удалённой из очереди книге
		}
	}
	if err := writeGroups(s.root, data); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeGroupViews(w, cfg, data)
}

func (s *server) handleSetBookGroup(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var req struct {
		Group string `json:"group"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	s.glossaryMu.Lock()
	defer s.glossaryMu.Unlock()
	data, err := readGroups(s.root)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !data.has(req.Group) {
		writeError(w, http.StatusBadRequest, "Такой группы нет")
		return
	}
	if req.Group == defaultGroup {
		delete(data.Books, file.Hash)
	} else {
		data.Books[file.Hash] = req.Group
	}
	if err := writeGroups(s.root, data); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeGroupViews(w, cfg, data)
}
