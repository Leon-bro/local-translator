package main

import (
	"bytes"
	"database/sql"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestClassifyMatchesPythonRule(t *testing.T) {
	done := func(flagged bool, reasons string) *chunkRow {
		return &chunkRow{Status: "done", Flagged: flagged, Reasons: reasons}
	}
	cases := []struct {
		name          string
		first, second *chunkRow
		state         string
		chooseSecond  bool
	}{
		{"pending", &chunkRow{Status: "pending"}, nil, statePending, false},
		{"error", &chunkRow{Status: "error"}, nil, stateError, false},
		{"clean", done(false, ""), nil, stateFirst, false},
		{"flagged, waits", done(true, "a"), &chunkRow{Status: "pending"}, stateFlagged, false},
		{"second fixes", done(true, "a"), done(false, ""), stateSecond, true},
		{"second equal keeps second", done(true, "a"), done(true, "b"), stateStillFlagd, true},
		{"second worse keeps first", done(true, "a"), done(true, "a; b"), stateStillFlagd, false},
	}
	for _, c := range cases {
		state, chosen := classify(c.first, c.second)
		if state != c.state {
			t.Errorf("%s: state %q, want %q", c.name, state, c.state)
		}
		if c.chooseSecond && chosen != c.second {
			t.Errorf("%s: должна быть выбрана вторая модель", c.name)
		}
		if !c.chooseSecond && c.first.Status == "done" && chosen != c.first {
			t.Errorf("%s: должна быть выбрана первая модель", c.name)
		}
	}
}

func TestSameModelOnBothPasses(t *testing.T) {
	row := &chunkRow{Status: "done", Flagged: true, Reasons: "a"}
	if state, chosen := classify(row, row); state != stateStillFlagd || chosen != row {
		t.Fatalf("state %q", state)
	}
}

func TestStaleAfterModelChange(t *testing.T) {
	root := t.TempDir()
	cfg := &Config{}
	cfg.Paths.DB = "state.db"
	cfg.Paths.Output = "out"
	cfg.LLM.PromptVer = 1
	cfg.LLM.DefaultMode = "two"
	cfg.LLM.Modes = []Mode{{ID: "two", Pass1: "m:4b", Pass2: "m:12b"}, {ID: "top", Pass1: "m:12b", Pass2: "m:12b"}}

	rw, err := sql.Open("sqlite", filepath.Join(root, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer rw.Close()
	_, err = rw.Exec(`CREATE TABLE files (file_hash TEXT, path TEXT, name TEXT, status TEXT, mode TEXT, rendered_mode TEXT);
		CREATE TABLE chunks (file_hash TEXT, block_id INT, part INT, model TEXT, prompt_ver INT, src TEXT, dst TEXT,
			status TEXT, flagged INT, reasons TEXT, seconds REAL);
		INSERT INTO files VALUES ('h1', 'a.pdf', 'a.pdf', 'rendered', 'two', 'two'), ('h2', 'pics.pdf', 'pics.pdf', 'rendered', 'two', 'two');
		INSERT INTO chunks VALUES
			('h1', 0, 0, 'm:4b', 1, 's', 'd', 'done', 0, NULL, 1),
			('h1', 1, 0, 'm:4b', 1, 's', 'd', 'done', 1, 'r', 1),
			('h1', 1, 0, 'm:12b', 1, 's', 'd', 'done', 0, NULL, 5);`)
	if err != nil {
		t.Fatal(err)
	}
	stale := func() map[string]bool {
		db, err := openDB(root, cfg)
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		files, err := listFiles(db, root, cfg)
		if err != nil {
			t.Fatal(err)
		}
		return map[string]bool{files[0].Hash: files[0].Stale, files[1].Hash: files[1].Stale}
	}
	if got := stale(); got["h1"] || got["h2"] {
		t.Fatalf("при текущих настройках книга не устарела: %v", got)
	}
	// PDF собран другим режимом, даже если все куски на месте.
	if _, err := rw.Exec(`UPDATE files SET rendered_mode = 'top' WHERE file_hash = 'h1'`); err != nil {
		t.Fatal(err)
	}
	if got := stale(); !got["h1"] {
		t.Fatalf("PDF другого режима считается устаревшим: %v", got)
	}
	// Выбран режим, для которого переведён один кусок из двух.
	if _, err := rw.Exec(`UPDATE files SET mode = 'top' WHERE file_hash = 'h1'`); err != nil {
		t.Fatal(err)
	}
	if got := stale(); !got["h1"] || got["h2"] {
		t.Fatalf("устаревать должна только книга с неполным переводом: %v", got)
	}
}

func TestMergeTerms(t *testing.T) {
	merged, added, updated := mergeTerms(
		[]Term{{"Hobbit", "хоббит"}, {"Shire", "Шир"}},
		[]Term{{"Shire", "Хоббитания"}, {" Frodo\t", " Фродо "}, {"Hobbit", "хоббит"}, {"", "x"}, {"y", ""}, {"Frodo", "Фродо"}},
	)
	want := []Term{{"Hobbit", "хоббит"}, {"Shire", "Хоббитания"}, {"Frodo", "Фродо"}}
	if len(merged) != 3 || merged[0] != want[0] || merged[1] != want[1] || merged[2] != want[2] || added != 1 || updated != 1 {
		t.Fatalf("%v added=%d updated=%d", merged, added, updated)
	}
}

func TestGlossaryRoundTrip(t *testing.T) {
	raw := "# комментарий\nHobbit\tхоббит\r\n\nбез табуляции\nShire\tШир\tлишнее\n"
	terms, comments := parseGlossary([]byte(raw))
	if len(terms) != 2 || terms[1] != (Term{"Shire", "Шир"}) || len(comments) != 1 {
		t.Fatalf("%v %v", terms, comments)
	}
	got := string(formatGlossary(terms, comments))
	if got != "# комментарий\nHobbit\tхоббит\nShire\tШир\n" {
		t.Fatalf("%q", got)
	}
	// Перевод строки в термине не должен создавать новую запись.
	if got := string(formatGlossary([]Term{{"a\nb", "c\td"}}, []string{"# x\ny", "не комментарий"})); got != "a b\tc d\n" {
		t.Fatalf("%q", got)
	}
}

func TestSafeFileName(t *testing.T) {
	cases := map[string]string{
		"book.pdf":                   "book.pdf",
		`..\..\evil.pdf`:             "evil.pdf",
		"../../etc/evil.pdf":         "evil.pdf",
		`C:\Users\x\Война и мир.pdf`: "Война и мир.pdf",
		`a:b?c.pdf`:                  "a_b_c.pdf",
		"..":                         "",
	}
	for in, want := range cases {
		if got := safeFileName(in); got != want {
			t.Errorf("safeFileName(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSaveModesKeepsComments(t *testing.T) {
	root := t.TempDir()
	original := `llm:
  default_mode: a # для новых книг
  modes:
    - id: a
      label: Первый
      pass1_model: m:4b   # комментарий
      pass2_model: m:4b
    - id: b
      pass1_model: m:12b
      pass2_model: m:12b
  other: 1
`
	path := filepath.Join(root, "config.yaml")
	if err := os.WriteFile(path, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveModes(root, []Mode{{ID: "b", Pass1: "x:1", Pass2: "y:2"}}, "b"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	want := strings.NewReplacer(
		"default_mode: a #", "default_mode: b #",
		"pass1_model: m:12b", "pass1_model: x:1",
		"pass2_model: m:12b", "pass2_model: y:2",
	).Replace(original)
	if string(raw) != want {
		t.Fatalf("got:\n%s", raw)
	}
	if err := saveModes(root, []Mode{{ID: "missing", Pass1: "x", Pass2: "y"}}, "a"); err == nil {
		t.Fatal("несуществующий режим должен давать ошибку")
	}
}

func TestDownloadHeader(t *testing.T) {
	got := downloadHeader("Война и мир.pdf")
	_, params, err := mime.ParseMediaType(got)
	if err != nil || params["filename"] != "Война и мир (перевод).pdf" || !strings.HasPrefix(got, "attachment") {
		t.Fatalf("%q: %v %v", got, params, err)
	}
}

func TestGuard(t *testing.T) {
	handler := guard("127.0.0.1:8765", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	cases := []struct {
		method, host, origin string
		status               int
	}{
		{"GET", "127.0.0.1:8765", "", 200},
		{"GET", "evil.example:8765", "", 403},
		{"POST", "127.0.0.1:8765", "http://evil.example", 403},
		{"POST", "127.0.0.1:8765", "http://127.0.0.1:8765", 200},
		{"POST", "localhost:8765", "http://localhost:8765", 200},
		{"DELETE", "127.0.0.1:8765", "http://127.0.0.1:9999", 403},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, "/api/x", nil)
		req.Host = c.host
		if c.origin != "" {
			req.Header.Set("Origin", c.origin)
		}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != c.status {
			t.Errorf("%s host=%s origin=%s: %d, want %d", c.method, c.host, c.origin, rec.Code, c.status)
		}
	}
}

func TestSaveUpload(t *testing.T) {
	dir := t.TempDir()
	s := &server{}
	pdf := []byte("%PDF-1.4 first")
	first, err := s.saveUpload(bytes.NewReader(pdf), "b.pdf", dir)
	if err != nil || filepath.Base(first) != "b.pdf" {
		t.Fatalf("%v %v", first, err)
	}
	same, err := s.saveUpload(bytes.NewReader(pdf), "b.pdf", dir)
	if err != nil || same != first {
		t.Fatalf("тот же файл не должен дублироваться: %v %v", same, err)
	}
	other, err := s.saveUpload(bytes.NewReader([]byte("%PDF-1.4 second")), "b.pdf", dir)
	if err != nil || filepath.Base(other) != "b (2).pdf" {
		t.Fatalf("другой файл с тем же именем: %v %v", other, err)
	}
	if _, err := s.saveUpload(strings.NewReader("not a pdf"), "c.pdf", dir); err == nil {
		t.Fatal("не-PDF должен отклоняться")
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 2 {
		t.Fatalf("в папке остался мусор: %v", entries)
	}
}

func TestUploadRejectsNonPDF(t *testing.T) {
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "config.yaml"), []byte("paths:\n  input_dir: in\n"), 0o644)
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	part, _ := form.CreateFormFile("files", "notes.txt")
	part.Write([]byte("hello"))
	form.Close()
	req := httptest.NewRequest("POST", "/api/upload", &body)
	req.Header.Set("Content-Type", form.FormDataContentType())
	rec := httptest.NewRecorder()
	s := &server{root: root, hub: newLogHub()}
	s.handleUpload(rec, req)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "нужен файл .pdf") {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

func TestSaveKeepNames(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "config.yaml")
	if err := os.WriteFile(path, []byte("llm:\n  keep_names: true   # комментарий\n  host: x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveKeepNames(root, false); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if string(raw) != "llm:\n  keep_names: false   # комментарий\n  host: x\n" {
		t.Errorf("config.yaml: %q", raw)
	}
	if err := os.WriteFile(path, []byte("llm:\n  host: x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveKeepNames(root, true); err == nil {
		t.Error("ожидалась ошибка: настройки нет")
	}
}

func TestNotesKeepOrderAndCreationDate(t *testing.T) {
	notes := upsertNote(nil, ReaderNote{Index: 5, Text: "пять", CreatedAt: 100})
	notes = upsertNote(notes, ReaderNote{Index: 2, Text: "два", CreatedAt: 200})
	notes = upsertNote(notes, ReaderNote{Index: 5, Text: "новый текст", CreatedAt: 300})
	if len(notes) != 2 || notes[0].Index != 2 || notes[1].Text != "новый текст" || notes[1].CreatedAt != 100 {
		t.Errorf("заметки: %+v", notes)
	}
	notes = removeNote(notes, 2)
	if len(notes) != 1 || notes[0].Index != 5 {
		t.Errorf("после удаления: %+v", notes)
	}
}

func TestDamagedNotesFileIsNotOverwritten(t *testing.T) {
	path := filepath.Join(t.TempDir(), "notes.json")
	if notes, err := readNotes(path); err != nil || len(notes) != 0 {
		t.Errorf("нет файла: %v %v", notes, err)
	}
	if err := os.WriteFile(path, []byte("{сломано"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readNotes(path); err == nil {
		t.Error("повреждённый файл должен давать ошибку")
	}
	if got := truncateRunes("абвгд", 3); got != "абв" {
		t.Errorf("обрезка: %q", got)
	}
}

func TestSetCharacter(t *testing.T) {
	raw := setCharacter(nil, "Shuna", "f")
	raw = setCharacter(raw, "Vega", "m")
	raw = setCharacter(raw, "Shuna", "m")
	if string(raw) != "Shuna\tm\nVega\tm\n" {
		t.Fatalf("замена: %q", raw)
	}
	raw = setCharacter([]byte("# заметка\n"+string(raw)), "Shuna", "")
	if string(raw) != "# заметка\nVega\tm\n" {
		t.Fatalf("сброс: %q", raw)
	}
}

func TestGroupsFile(t *testing.T) {
	root := t.TempDir()
	data, err := readGroups(root)
	if err != nil || len(data.Groups) != 0 || data.of("x") != defaultGroup {
		t.Fatalf("пустой файл: %v %v", data, err)
	}
	data.Groups = append(data.Groups, Group{"slime", "Slime"})
	data.Books["x"] = "slime"
	data.Books["y"] = "gone"
	if err := writeGroups(root, data); err != nil {
		t.Fatal(err)
	}
	again, err := readGroups(root)
	if err != nil || again.of("x") != "slime" || again.of("y") != defaultGroup {
		t.Fatalf("чтение: %v %v", again, err)
	}
	if !again.nameTaken("SLIME", "") || again.nameTaken("slime", "slime") || !again.nameTaken(defaultGroupName, "") {
		t.Fatal("проверка названия")
	}
	if err := os.WriteFile(filepath.Join(root, groupsFileName), []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readGroups(root); err == nil {
		t.Fatal("повреждённый файл должен быть ошибкой")
	}
}

func TestContextPaths(t *testing.T) {
	cfg := &Config{}
	cfg.Paths.Glossary, cfg.Paths.Hints, cfg.Paths.PlainNames = "glossary.tsv", "hints.tsv", "plain_names.txt"
	if got := contextFor("r", cfg, defaultGroup); got.Glossary != filepath.Join("r", "glossary.tsv") || got.Characters != filepath.Join("r", "characters.tsv") {
		t.Fatalf("по умолчанию: %+v", got)
	}
	if got := contextFor("r", cfg, "slime"); got.Hints != filepath.Join("r", "context", "slime", "hints.tsv") {
		t.Fatalf("группа: %+v", got)
	}
}
