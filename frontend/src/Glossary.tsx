import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, BookGroup, Candidate, Character, FileInfo, Term } from "./api";
import { Groups } from "./Groups";
import { Characters } from "./Characters";
import { buildNamesRequest, matchAnswer, parseList } from "./termList";

interface Props {
  files: FileInfo[];
  initialBook: string | null;
  jobRunning: boolean;
}

interface BodyProps {
  files: FileInfo[];
  initialBook: string | null;
  group: string;
  onDirty: (dirty: boolean) => void;
  onTermsChanged: () => void;
}

/** Страница группы книг: у каждой группы свой глоссарий и всё остальное. */
export function Glossary({ files, initialBook, jobRunning }: Props) {
  const [groups, setGroups] = useState<BookGroup[] | null>(null);
  const [group, setGroup] = useState("default");
  const [error, setError] = useState("");
  const dirty = useRef(false);
  const onDirty = useCallback((value: boolean) => { dirty.current = value; }, []);

  const reload = useCallback(() => {
    api.groups().then((r) => setGroups(r.groups)).catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    api.groups()
      .then((r) => {
        setGroups(r.groups);
        const owner = r.groups.find((g) => initialBook && g.books.includes(initialBook));
        if (owner) setGroup(owner.id);
      })
      .catch((e: Error) => setError(e.message));
  }, [initialBook]);

  const select = (id: string) => {
    if (dirty.current && !window.confirm("В таблице терминов есть несохранённые изменения. Перейти к другой группе без сохранения?")) return;
    dirty.current = false;
    setGroup(id);
  };

  if (error) return <section className="settings" aria-label="Глоссарий"><h1>Глоссарий</h1><p className="banner banner-error" role="alert">{error}</p></section>;
  if (!groups) return <section className="settings" aria-label="Глоссарий"><h1>Глоссарий</h1><p className="hint">Загружаю группы книг.</p></section>;
  const books = groups.find((g) => g.id === group)?.books ?? [];
  return (
    <section className="settings" aria-label="Глоссарий">
      <h1>Глоссарий</h1>
      <p className="hint">
        Модель получает только термины из переводимого куска. Перевод указывайте в начальной форме, падеж
        она подберёт сама. Термин можно добавить и прямо из текста: выделите слово на странице книги.
      </p>
      <Groups
        groups={groups}
        current={group}
        files={files}
        jobRunning={jobRunning}
        onSelect={select}
        onChanged={(next, target) => {
          setGroups(next);
          if (target) { dirty.current = false; setGroup(target); }
        }}
      />
      <GlossaryBody
        key={group}
        files={files.filter((f) => books.includes(f.hash))}
        initialBook={initialBook}
        group={group}
        onDirty={onDirty}
        onTermsChanged={reload}
      />
    </section>
  );
}

function GlossaryBody({ files, initialBook, group, onDirty, onTermsChanged }: BodyProps) {
  const [terms, setTerms] = useState<Term[] | null>(null);
  const [saved, setSaved] = useState("");
  const [comments, setComments] = useState<string[] | null>(null);
  const [hints, setHints] = useState({ count: 0, file: "" });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const sourceInput = useRef<HTMLInputElement>(null);

  const [list, setList] = useState("");
  const parsed = useMemo(() => parseList(list), [list]);
  const fileInput = useRef<HTMLInputElement>(null);

  const [book, setBook] = useState(initialBook && files.some((f) => f.hash === initialBook) ? initialBook : files[0]?.hash ?? "");
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [plain, setPlain] = useState<string[]>([]);
  const [characters, setCharacters] = useState<Character[] | null>(null);
  const [known, setKnown] = useState<Term[]>([]);
  const [answer, setAnswer] = useState("");
  const parsedAnswer = useMemo(() => parseList(answer), [answer]);
  const answerFile = useRef<HTMLInputElement>(null);
  const [keepNames, setKeepNames] = useState(false);
  const [namesVersion, setNamesVersion] = useState(0);
  const [loadingNames, setLoadingNames] = useState(false);
  const [namesError, setNamesError] = useState("");

  const load = async () => {
    const data = await api.glossary(group);
    setTerms(data.terms);
    setSaved(JSON.stringify(data.terms));
    setComments(data.comments);
    setHints({ count: data.hints, file: data.hintsFile });
  };

  useEffect(() => { load().catch((e: Error) => setError(e.message)); }, [group]);

  // Книгу перенесли из группы: выбираем другую из оставшихся.
  useEffect(() => {
    if (!files.some((f) => f.hash === book)) setBook(files[0]?.hash ?? "");
  }, [files, book]);

  useEffect(() => {
    if (!book) { setCandidates([]); setCharacters([]); return; }
    let cancelled = false;
    setLoadingNames(true);
    setCandidates(null);
    setCharacters(null);
    setNamesError("");
    api.candidates(book)
      .then((data) => {
        if (cancelled) return;
        setCandidates(data.candidates);
        setPlain(data.plain ?? []);
        setCharacters(data.characters ?? []);
        setKnown(data.known ?? []);
        setKeepNames(data.keepNames);
      })
      .catch((e: Error) => { if (!cancelled) setNamesError(e.message); })
      .finally(() => { if (!cancelled) setLoadingNames(false); });
    return () => { cancelled = true; };
  }, [book, namesVersion]);

  const dirty = terms !== null && JSON.stringify(terms) !== saved;
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);

  /** Добавляет на сервере и не трогает несохранённые правки в таблице. */
  const addTerms = async (items: Term[]) => {
    setError(""); setMessage("");
    const result = await api.addTerms(items, group);
    onTermsChanged();
    const merge = (current: Term[]) => {
      const next = [...current];
      for (const item of items) {
        const at = next.findIndex((t) => t.source === item.source);
        if (at >= 0) next[at] = item;
        else next.unshift(item);
      }
      return next;
    };
    setTerms((current) => merge(current ?? []));
    setSaved((current) => JSON.stringify(merge(JSON.parse(current || "[]"))));
    const parts = [];
    if (result.added) parts.push(`добавлено ${result.added}`);
    if (result.updated) parts.push(`обновлено ${result.updated}`);
    setMessage(`Глоссарий: ${parts.join(", ") || "без изменений"}. Чтобы применить к готовой книге, нажмите «Применить глоссарий» на её странице.`);
  };

  const run = (action: () => Promise<void>) => action().catch((e: Error) => setError(e.message));

  const addOne = () => run(async () => {
    await addTerms([{ source: source.trim(), target: target.trim() }]);
    setSource(""); setTarget("");
    sourceInput.current?.focus();
  });

  const addList = () => run(async () => {
    await addTerms(parsed.terms);
    setList(parsed.failed.join("\n"));
  });

  const readFile = (file: File) => run(async () => {
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      setList(await file.text());
      return;
    }
    if (dirty) {
      setError("Сначала сохраните изменения в таблице терминов: импорт обновит её.");
      return;
    }
    setError(""); setMessage("");
    const r = await api.importGlossary(file, group);
    await load();
    onTermsChanged();
    const skipped = r.skipped?.length ? ` Пропущено ${r.skipped.length}: ${r.skipped.slice(0, 8).join("; ")}${r.skipped.length > 8 ? "…" : ""}.` : "";
    setMessage(`Из таблицы: терминов добавлено ${r.added}, обновлено ${r.updated}; подсказок добавлено ${r.hintsAdded}, обновлено ${r.hintsUpdated}.${skipped}`);
  });

  const filled = (candidates ?? []).filter((c) => drafts[c.term]?.trim());
  const addNames = (items: Candidate[]) => run(async () => {
    await addTerms(items.map((c) => ({ source: c.term, target: drafts[c.term].trim() })));
    const added = new Set(items.map((c) => c.term));
    setCandidates((current) => (current ?? []).filter((c) => !added.has(c.term)));
  });

  const bookName = (files.find((f) => f.hash === book)?.name ?? "книга").replace(/\.pdf$/i, "");
  const namesRequest = () => buildNamesRequest(bookName, candidates ?? [], known);

  const downloadRequest = () => {
    const url = URL.createObjectURL(new Blob([namesRequest()], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${bookName} - имена для перевода.txt`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const copyRequest = () => run(async () => {
    setError(""); setMessage("");
    await navigator.clipboard.writeText(namesRequest());
    setMessage("Запрос скопирован. Вставьте его в чат другой модели, а её ответ верните сюда.");
  });

  /** Переводы попадают в таблицу на проверку, в глоссарий их добавляет кнопка «Добавить заполненные». */
  const applyAnswer = () => {
    const { matched, other } = matchAnswer(parsedAnswer.terms, candidates ?? []);
    setDrafts((current) => ({ ...current, ...Object.fromEntries(matched.map((t) => [t.source, t.target])) }));
    const rest = other.map((t) => `${t.source} = ${t.target}`);
    if (rest.length) setList((current) => [current.trim(), ...rest].filter(Boolean).join("\n"));
    const missing = (candidates?.length ?? 0) - new Set(matched.map((t) => t.source)).size;
    const parts = [`Заполнено строк в таблице: ${new Set(matched.map((t) => t.source)).size}. Проверьте их и нажмите «Добавить заполненные».`];
    if (missing > 0) parts.push(`Без перевода осталось ${missing}.`);
    if (rest.length) parts.push(`Терминов не из этого списка: ${rest.length}, они перенесены в «Добавить списком».`);
    setError(""); setMessage(parts.join(" "));
    setAnswer(parsedAnswer.failed.join("\n"));
  };

  const savePlain = (next: string[]) => run(async () => {
    await api.savePlainNames(next, group);
    setNamesVersion((v) => v + 1);
  });

  const saveTable = () => run(async () => {
    if (!terms) return;
    setError(""); setMessage("");
    const clean = terms.filter((t) => t.source.trim() && t.target.trim());
    await api.saveGlossary(clean, comments, group);
    onTermsChanged();
    setTerms(clean);
    setSaved(JSON.stringify(clean));
    setMessage("Изменения сохранены. Чтобы применить к готовой книге, нажмите «Применить глоссарий» на её странице.");
  });

  const update = (index: number, patch: Partial<Term>) =>
    setTerms((current) => current!.map((t, i) => (i === index ? { ...t, ...patch } : t)));

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (terms ?? [])
      .map((term, index) => ({ term, index }))
      .filter(({ term }) => !q || term.source.toLowerCase().includes(q) || term.target.toLowerCase().includes(q));
  }, [terms, query]);

  return (
    <>
      {message && <p className="banner banner-ok" role="status">{message}</p>}
      {error && <p className="banner banner-error" role="alert">{error}</p>}

      <form className="quick-add" onSubmit={(e) => { e.preventDefault(); if (source.trim() && target.trim()) addOne(); }}>
        <label className="field"><span>В оригинале</span>
          <input ref={sourceInput} value={source} onChange={(e) => setSource(e.target.value)} placeholder="Hobbit" autoFocus />
        </label>
        <label className="field"><span>В переводе</span>
          <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="хоббит" />
        </label>
        <button className="button button-primary" disabled={!source.trim() || !target.trim()}>Добавить термин</button>
      </form>

      <div className="settings-block">
        <h2>Имена из книги</h2>
        {files.length === 0 && <p className="hint">Перенесите в эту группу книгу, и здесь появятся самые частые имена из неё.</p>}
        {files.length > 0 && (
          <>
            <p className="hint">
              {keepNames
                ? "Имена и названия, которых нет в глоссарии. Без перевода в этой таблице они останутся в книге по-английски. Если здесь оказалось обычное слово, нажмите «Переводить обычно»."
                : "Самые частые имена и названия, которых ещё нет в глоссарии. Лучше заполнить их до перевода."}
            </p>
            <div className="names-tools">
              <select aria-label="Книга" value={book} onChange={(e) => setBook(e.target.value)}>
                {files.map((f) => <option key={f.hash} value={f.hash}>{f.name.replace(/\.pdf$/i, "")}</option>)}
              </select>
              <button className="button button-primary" disabled={!filled.length} onClick={() => addNames(filled)}>
                Добавить заполненные{filled.length ? ` (${filled.length})` : ""}
              </button>
            </div>
            {loadingNames && <p className="hint">Ищу имена в книге.</p>}
            {namesError && <p className="banner banner-error" role="alert">{namesError}</p>}
            {candidates && candidates.length === 0 && <p className="hint">Новых имён не нашлось: все частые уже в глоссарии.</p>}
            {candidates && candidates.length > 0 && (
              <table className="names">
                <thead><tr><th scope="col">Имя</th><th scope="col">Раз</th><th scope="col">Перевод</th><th scope="col">Где встречается</th><th scope="col"><span className="visually-hidden">Действие</span></th></tr></thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.term}>
                      <td className="name-term">{c.term}</td>
                      <td>{c.count}</td>
                      <td>
                        <input
                          aria-label={`Перевод: ${c.term}`}
                          value={drafts[c.term] ?? ""}
                          onChange={(e) => setDrafts((d) => ({ ...d, [c.term]: e.target.value }))}
                          onKeyDown={(e) => { if (e.key === "Enter" && drafts[c.term]?.trim()) addNames([c]); }}
                        />
                      </td>
                      <td className="name-example">{c.example}</td>
                      <td><button type="button" className="button button-quiet" aria-label={`Переводить обычно: ${c.term}`} onClick={() => savePlain([...plain, c.term])}>Переводить обычно</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {candidates && candidates.length > 0 && (
              <div className="names-exchange">
                <h3>Перевести другой моделью</h3>
                <p className="hint">
                  Скачайте список или скопируйте запрос и отправьте его другой модели. Её ответ вставьте сюда или загрузите
                  файлом: переводы появятся в таблице выше, и вы сможете их проверить перед добавлением в глоссарий.
                </p>
                <div className="names-tools">
                  <button type="button" className="button" onClick={downloadRequest}>Скачать список</button>
                  <button type="button" className="button" onClick={copyRequest}>Скопировать запрос</button>
                </div>
                <textarea
                  className="term-list"
                  aria-label="Ответ другой модели"
                  rows={5}
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  placeholder={"Adapter = Адаптер\nVega = Вега"}
                />
                {parsedAnswer.failed.length > 0 && <p className="note note-error">Не понял строки: {parsedAnswer.failed.slice(0, 3).join("; ")}{parsedAnswer.failed.length > 3 ? ` и ещё ${parsedAnswer.failed.length - 3}` : ""}</p>}
                <div className="names-tools">
                  <button type="button" className="button button-primary" disabled={!parsedAnswer.terms.length} onClick={applyAnswer}>
                    Заполнить таблицу{parsedAnswer.terms.length ? ` (${parsedAnswer.terms.length})` : ""}
                  </button>
                  <button type="button" className="button" onClick={() => answerFile.current?.click()}>Загрузить ответ</button>
                  <input
                    ref={answerFile}
                    type="file"
                    accept=".txt,.tsv,.csv,.md,text/plain,text/csv,text/markdown"
                    hidden
                    aria-label="Файл с ответом другой модели"
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) run(async () => setAnswer(await f.text())); e.target.value = ""; }}
                  />
                </div>
              </div>
            )}
            {plain.length > 0 && (
              <div className="plain-names">
                <h3>Переводятся обычно</h3>
                <ul>
                  {plain.map((name) => (
                    <li key={name}>
                      {name}
                      <button type="button" className="button button-quiet" aria-label={`Снова оставлять по-английски: ${name}`} onClick={() => savePlain(plain.filter((n) => n !== name))}>Вернуть</button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>

      {files.length > 0 && <Characters characters={characters} bookName={bookName} group={group} />}

      <div className="settings-block">
        <h2>Добавить списком</h2>
        <p className="hint">По одному термину в строке: «Hobbit = хоббит». Подойдут строки из Excel и файлы .tsv, .csv, .txt. Файл .xlsx импортируется сразу: имена станут терминами, а лексика и идиомы подсказками.</p>
        <textarea
          className="term-list"
          aria-label="Список терминов"
          rows={5}
          value={list}
          onChange={(e) => setList(e.target.value)}
          placeholder={"Frodo = Фродо\nMordor = Мордор\nShire = Шир"}
        />
        {parsed.failed.length > 0 && <p className="note note-error">Не понял строки: {parsed.failed.slice(0, 3).join("; ")}{parsed.failed.length > 3 ? ` и ещё ${parsed.failed.length - 3}` : ""}</p>}
        <div className="names-tools">
          <button className="button button-primary" disabled={!parsed.terms.length} onClick={addList}>
            Добавить из списка{parsed.terms.length ? ` (${parsed.terms.length})` : ""}
          </button>
          <button className="button" onClick={() => fileInput.current?.click()}>Загрузить файл</button>
          <input
            ref={fileInput}
            type="file"
            accept=".xlsx,.tsv,.csv,.txt,text/plain,text/csv"
            hidden
            aria-label="Файл глоссария"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) readFile(f); e.target.value = ""; }}
          />
        </div>
      </div>

      <div className="settings-block">
        <div className="settings-head">
          <h2>Все термины{terms ? ` (${terms.length})` : ""}</h2>
          <button className="button button-primary" disabled={!dirty} onClick={saveTable}>Сохранить изменения</button>
        </div>
        {hints.count > 0 && (
          <p className="hint">
            Ещё {hints.count} подсказок для модели, например лексика и идиомы. Проверка их не требует: модель вправе
            выбрать другой вариант. Они лежат в файле {hints.file}.
          </p>
        )}
        {terms !== null && terms.length > 0 && (
          <input className="search search-full" type="search" placeholder="Найти термин" aria-label="Найти термин" value={query} onChange={(e) => setQuery(e.target.value)} />
        )}
        {terms !== null && (
          <table className="glossary">
            {rows.length > 0 && <thead><tr><th scope="col">В оригинале</th><th scope="col">В переводе</th><th /></tr></thead>}
            <tbody>
              {rows.map(({ term, index }) => (
                <tr key={`${term.source}-${index}`}>
                  <td><input aria-label="Термин в оригинале" defaultValue={term.source} onBlur={(e) => update(index, { source: e.target.value })} /></td>
                  <td><input aria-label="Термин в переводе" defaultValue={term.target} onBlur={(e) => update(index, { target: e.target.value })} /></td>
                  <td><button className="link" aria-label={`Удалить термин ${term.source}`} onClick={() => setTerms(terms.filter((_, i) => i !== index))}>Удалить</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {terms !== null && rows.length === 0 && <p className="hint">{query ? "Таких терминов нет." : "Глоссарий пуст. Начните с имён из книги выше."}</p>}
      </div>
    </>
  );
}
