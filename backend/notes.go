package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	maxNoteRunes  = 5000
	maxQuoteRunes = 200
)

// ReaderNote — заметка читателя к абзацу. Quote хранит начало исходного текста: если книгу разберут
// заново и номера кусков сдвинутся, заметка найдёт свой абзац по нему.
type ReaderNote struct {
	Index     int    `json:"index"`
	Text      string `json:"text"`
	Quote     string `json:"quote"`
	CreatedAt int64  `json:"createdAt"`
}

// upsertNote заменяет заметку на этом абзаце или добавляет новую. Дата создания сохраняется.
func upsertNote(notes []ReaderNote, note ReaderNote) []ReaderNote {
	out := make([]ReaderNote, 0, len(notes)+1)
	for _, existing := range notes {
		if existing.Index == note.Index {
			note.CreatedAt = existing.CreatedAt
			continue
		}
		out = append(out, existing)
	}
	out = append(out, note)
	sort.Slice(out, func(i, j int) bool { return out[i].Index < out[j].Index })
	return out
}

func removeNote(notes []ReaderNote, index int) []ReaderNote {
	out := make([]ReaderNote, 0, len(notes))
	for _, existing := range notes {
		if existing.Index != index {
			out = append(out, existing)
		}
	}
	return out
}

func truncateRunes(text string, limit int) string {
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}

func notesPath(root string, cfg *Config, hash string) string {
	return filepath.Join(root, cfg.Paths.WorkDir, hash, "notes.json")
}

// readNotes возвращает ошибку на повреждённом файле: иначе следующая запись стёрла бы все заметки.
func readNotes(path string) ([]ReaderNote, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return []ReaderNote{}, nil
	}
	if err != nil {
		return nil, err
	}
	notes := []ReaderNote{}
	if err := json.Unmarshal(raw, &notes); err != nil {
		return nil, fmt.Errorf("файл заметок повреждён (%s): %w", path, err)
	}
	return notes, nil
}

func writeNotes(path string, notes []ReaderNote) error {
	raw, err := json.MarshalIndent(notes, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return writeAtomic(path, raw)
}

func (s *server) handleGetNotes(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	s.notesMu.Lock()
	defer s.notesMu.Unlock()
	notes, err := readNotes(notesPath(s.root, cfg, file.Hash))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"notes": notes})
}

// changeNote читает заметки, применяет правку и записывает результат.
func (s *server) changeNote(w http.ResponseWriter, path string, change func([]ReaderNote) []ReaderNote) {
	s.notesMu.Lock()
	defer s.notesMu.Unlock()
	notes, err := readNotes(path)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	notes = change(notes)
	if err := writeNotes(path, notes); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"notes": notes})
}

func noteIndex(w http.ResponseWriter, r *http.Request) (int, bool) {
	index, err := strconv.Atoi(r.PathValue("index"))
	if err != nil || index < 0 {
		writeError(w, http.StatusBadRequest, "Неверный номер абзаца")
		return 0, false
	}
	return index, true
}

func (s *server) handlePutNote(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	index, ok := noteIndex(w, r)
	if !ok {
		return
	}
	var req struct {
		Text  string `json:"text"`
		Quote string `json:"quote"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256<<10)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Неверный запрос")
		return
	}
	text := strings.TrimSpace(req.Text)
	if len([]rune(text)) > maxNoteRunes {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("Заметка длиннее %d символов", maxNoteRunes))
		return
	}
	note := ReaderNote{Index: index, Text: text, Quote: truncateRunes(req.Quote, maxQuoteRunes), CreatedAt: time.Now().Unix()}
	s.changeNote(w, notesPath(s.root, cfg, file.Hash), func(notes []ReaderNote) []ReaderNote {
		return upsertNote(notes, note)
	})
}

func (s *server) handleDeleteNote(w http.ResponseWriter, r *http.Request) {
	file, cfg, ok := s.lookup(w, r)
	if !ok {
		return
	}
	index, ok := noteIndex(w, r)
	if !ok {
		return
	}
	s.changeNote(w, notesPath(s.root, cfg, file.Hash), func(notes []ReaderNote) []ReaderNote {
		return removeNote(notes, index)
	})
}
