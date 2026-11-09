import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, Block, BlockPart, FileInfo, Mode, UserNote } from "./api";
import { Ribbon } from "./Ribbon";

interface Props {
  file: FileInfo;
  modes: Mode[];
  ollamaModels: string[] | null;
  busy: boolean;
  onAction: (action: () => Promise<unknown>) => void;
  onRemove: () => void;
  onOpenGlossary: () => void;
}

interface Selected {
  text: string;
  side: "src" | "dst";
  x: number;
  y: number;
}

const POPOVER_WIDTH = 300;

/** Выделенный текст без знаков по краям: «Hobbits,» → «Hobbits». */
function selectedTerm(raw: string) {
  return raw.replace(/\s+/g, " ").replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
}

function readSelection(): Selected | null {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const text = selectedTerm(selection.toString());
  if (!text || text.length > 80) return null;
  const node = selection.anchorNode;
  const element = node instanceof Element ? node : node?.parentElement;
  if (element?.closest(".user-note")) return null;
  const side = element?.closest(".side-src") ? "src" : element?.closest(".side-dst") ? "dst" : null;
  if (!side) return null;
  const box = selection.getRangeAt(0).getBoundingClientRect();
  return {
    text,
    side,
    x: Math.max(8, Math.min(box.left, window.innerWidth - POPOVER_WIDTH - 8)),
    y: box.bottom + 6,
  };
}

/** Термин из выделения. Положение фиксированное: разворот прокручивается отдельно от страницы. */
function TermPopover({ pick, book, onClose }: { pick: Selected; book: string; onClose: (message?: string) => void }) {
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState(pick.side === "src" ? pick.text : "");
  const [target, setTarget] = useState(pick.side === "dst" ? pick.text : "");
  const [error, setError] = useState("");
  const first = useRef<HTMLInputElement>(null);

  useEffect(() => { if (open) first.current?.focus(); }, [open]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const result = await api.addBookTerms([{ source: source.trim(), target: target.trim() }], book);
      onClose(`${result.updated ? "Термин обновлён" : "Термин добавлен"}: ${source.trim()} = ${target.trim()}. Чтобы применить, нажмите «Применить глоссарий» или запустите перевод.`);
    } catch (err) { setError((err as Error).message); }
  };

  const style = { left: pick.x, top: Math.min(pick.y, window.innerHeight - (open ? 200 : 50)) };
  if (!open) {
    return (
      <button className="button button-primary term-popover" style={style} onMouseDown={(e) => e.preventDefault()} onClick={() => setOpen(true)}>
        В глоссарий
      </button>
    );
  }
  return (
    <form className="term-popover term-form" style={style} onSubmit={submit}>
      <label className="field"><span>В оригинале</span>
        <input ref={pick.side === "dst" ? first : undefined} value={source} onChange={(e) => setSource(e.target.value)} />
      </label>
      <label className="field"><span>В переводе</span>
        <input ref={pick.side === "src" ? first : undefined} value={target} onChange={(e) => setTarget(e.target.value)} />
      </label>
      {error && <p className="note note-error">{error}</p>}
      <div className="term-form-actions">
        <button className="button button-primary" disabled={!source.trim() || !target.trim()}>Добавить</button>
        <button type="button" className="button button-quiet" onClick={() => onClose()}>Отмена</button>
      </div>
    </form>
  );
}

const shortModel = (model?: string) => model?.split(":").pop()?.toUpperCase() ?? "";

function formatDuration(seconds: number) {
  if (seconds < 60) return "меньше минуты";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `около ${minutes} мин`;
  return `около ${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

function statusLine(file: FileInfo, busy: boolean) {
  if (file.stale) return "Выбранный режим ещё не применён. Нажмите «Перевести книгу».";
  if (file.status === "new") return "Ждёт разбора.";
  if (file.errors) return `Не удалось перевести кусков: ${file.errors}. Запустите перевод ещё раз.`;
  if (file.done < file.total || file.status === "pass1_done") {
    const left = file.remainingSeconds > 0 && busy ? `, осталось ${formatDuration(file.remainingSeconds)}` : "";
    return `Переведено ${file.done} из ${file.total}${left}.`;
  }
  const parts = [`Переведено ${file.done} из ${file.total}`];
  if (file.second) parts.push(`вторая модель исправила ${file.second}`);
  if (file.flagged) parts.push(`требуют проверки ${file.flagged}`);
  return parts.join(", ") + ".";
}

function Note({ part }: { part: BlockPart }) {
  if (part.state === "e") return <p className="note note-error">Не переведено: {part.reasons}</p>;
  if (part.state === "x" || part.state === "f") {
    return (
      <p className="note note-flag">
        {part.state === "f" ? "Ждёт второго прохода" : "Проверьте"}: {part.reasons}
      </p>
    );
  }
  return null;
}

const NOTE_QUOTE_CHARS = 80;
const NOTE_PREVIEW_CHARS = 90;

interface PartNotes {
  note?: UserNote;
  onAdd: (part: BlockPart) => void;
  onDelete: (note: UserNote) => void;
}

function BookmarkButton({ part, note, onAdd, onDelete }: { part: BlockPart } & PartNotes) {
  return note
    ? <button type="button" className="note-add bookmark-on" aria-label="Убрать закладку" title="Убрать закладку" onClick={() => onDelete(note)}>Закладка</button>
    : <button type="button" className="note-add" aria-label="Поставить закладку" title="Поставить закладку" onClick={() => onAdd(part)}>Закладка</button>;
}

/** Закладка ставится одним нажатием. Текст есть только у заметок из прошлой версии, он показывается как был. */
function Part({ part, type, level, note, onAdd, onDelete }: { part: BlockPart; type: Block["type"]; level?: number } & PartNotes) {
  const heading = type === "heading";
  const cls = heading ? `h h${level ?? 2}` : type === "caption" ? "caption" : "para";
  const pending = part.state === "p";
  return (
    <div className={`row state-${part.state}${note ? " row-bookmarked" : ""}`} id={`chunk-${part.index}`}>
      <div className="side side-src">
        <p className={cls}>{part.src}</p>
        <BookmarkButton part={part} note={note} onAdd={onAdd} onDelete={onDelete} />
      </div>
      <div className="side side-dst">
        {pending ? <p className={`${cls} waiting`}>Ещё не переведено</p> : <p className={cls}>{part.dst}</p>}
        {part.state === "b" && <span className="pencil">{shortModel(part.model)}</span>}
        <Note part={part} />
        {part.alt && (
          <details className="alt">
            <summary>Вариант {shortModel(part.altModel)}</summary>
            <p>{part.alt}</p>
          </details>
        )}
        {note?.text && <div className="user-note"><p>{note.text}</p></div>}
        <BookmarkButton part={part} note={note} onAdd={onAdd} onDelete={onDelete} />
      </div>
    </div>
  );
}

const POSITION_SAVE_DELAY_MS = 800;
const SCROLL_SETTLE_MS = 150;
const SCROLL_MAX_FRAMES = 40;

/**
 * Высота абзацев вне экрана только оценивается (content-visibility) и уточняется, когда они появляются.
 * Поэтому одной прокрутки мало: она промахивается, и чем длиннее книга, тем сильнее. Повторяем, пока абзац не встанет.
 */
function scrollToChunk(index: number, block: ScrollLogicalPosition) {
  let frames = 0;
  let last: number | null = null;
  const step = () => {
    const el = document.getElementById(`chunk-${index}`);
    if (!el) return;
    el.scrollIntoView({ block });
    const top = el.getBoundingClientRect().top;
    if (top !== last && ++frames < SCROLL_MAX_FRAMES) {
      last = top;
      requestAnimationFrame(step);
    }
  };
  step();
}

function inView(el: Element, box: DOMRect): boolean {
  const rect = el.getBoundingClientRect();
  return rect.bottom > box.top && rect.top < box.bottom;
}

/** Первый абзац, видимый в окне чтения. Абзацы идут по порядку, поэтому хватает двоичного поиска. */
function firstVisibleChunk(spread: HTMLElement): number | null {
  const rows = spread.querySelectorAll('[id^="chunk-"]');
  const top = spread.getBoundingClientRect().top;
  let low = 0;
  let high = rows.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (rows[middle].getBoundingClientRect().bottom > top) high = middle;
    else low = middle + 1;
  }
  return low < rows.length ? chunkIndex(rows[low]) : null;
}

function chunkIndex(el: Element | null | undefined): number | null {
  const match = el?.id.match(/^chunk-(\d+)$/);
  return match ? Number(match[1]) : null;
}

/** Абзац, на котором остановился читатель: его отмечает щелчок или прокрутка, на нём книга открывается снова. */
function useReadingPosition(hash: string, visible: Block[] | null, filtered: boolean) {
  const spread = useRef<HTMLDivElement>(null);
  const [current, setCurrent] = useState<number | null>(null);
  const [stored, setStored] = useState<number | null | undefined>(undefined);
  const restored = useRef(false);
  const latest = useRef<number | null>(null);
  const saved = useRef<number | null>(null);
  const saveTimer = useRef<number | undefined>(undefined);
  const scrollTimer = useRef<number | undefined>(undefined);

  const flush = useRef(() => {
    window.clearTimeout(saveTimer.current);
    if (latest.current !== null && latest.current !== saved.current) {
      saved.current = latest.current;
      api.savePosition(hash, latest.current).catch(() => { saved.current = null; });
    }
  }).current;

  // Переход к заметке не считается чтением: место остаётся прежним, пока читатель не вернётся или не щёлкнет абзац.
  const [away, setAway] = useState(false);
  const awayRef = useRef(false);
  const setAwayState = (value: boolean) => {
    awayRef.current = value;
    setAway(value);
  };

  const remember = (index: number) => {
    setAwayState(false);
    latest.current = index;
    setCurrent(index);
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(flush, POSITION_SAVE_DELAY_MS);
  };

  useEffect(() => {
    let cancelled = false;
    api.position(hash)
      .then((data) => { if (!cancelled) setStored(data.index); })
      .catch(() => { if (!cancelled) setStored(null); });
    window.addEventListener("pagehide", flush);
    return () => {
      cancelled = true;
      window.clearTimeout(scrollTimer.current);
      window.removeEventListener("pagehide", flush);
      flush();
    };
  }, [hash, flush]);

  // Текст обновляется с каждым переведённым куском, а возвращаться к абзацу нужно только при открытии книги.
  useEffect(() => {
    if (restored.current || !visible?.length || stored === undefined || filtered) return;
    restored.current = true;
    if (stored === null) return;
    if (!document.getElementById(`chunk-${stored}`)) return;
    latest.current = stored;
    saved.current = stored;
    setCurrent(stored);
    scrollToChunk(stored, "start");
  }, [visible, stored, filtered]);

  useEffect(() => {
    const el = current === null ? null : document.getElementById(`chunk-${current}`);
    el?.setAttribute("data-current", "");
    return () => el?.removeAttribute("data-current");
  }, [current, visible]);

  const onScroll = () => {
    window.clearTimeout(scrollTimer.current);
    scrollTimer.current = window.setTimeout(() => {
      const box = spread.current;
      if (!box || !restored.current || filtered || awayRef.current) return;
      // Пока отмеченный абзац на экране, читатель всё ещё на нём.
      const marked = latest.current === null ? null : document.getElementById(`chunk-${latest.current}`);
      if (marked && inView(marked, box.getBoundingClientRect())) return;
      const top = firstVisibleChunk(box);
      if (top !== null) remember(top);
    }, SCROLL_SETTLE_MS);
  };

  const onClick = (target: EventTarget) => {
    const element = target as Element;
    // Работа с заметкой не означает, что читатель продолжил с этого абзаца.
    if (element.closest?.(".user-note, .note-add")) return;
    const index = chunkIndex(element.closest?.('[id^="chunk-"]'));
    if (index !== null) remember(index);
  };

  /** Уходим к заметке. Если места чтения ещё нет, им становится то, что сейчас на экране. */
  const leave = () => {
    if (latest.current === null && spread.current) {
      const top = firstVisibleChunk(spread.current);
      if (top !== null) remember(top);
    }
    setAwayState(true);
  };

  return { spread, current, away, leave, comeBack: () => setAwayState(false), remember, onScroll, onClick };
}

function ModePicker({ file, modes, ollamaModels, busy, onAction }: Omit<Props, "onRemove" | "onOpenGlossary">) {
  const [saving, setSaving] = useState<string | null>(null);
  const shown = saving ?? file.mode;
  const pick = (id: string) => {
    setSaving(id);
    onAction(() => api.setMode(file.hash, id).finally(() => setSaving(null)));
  };
  return (
    <fieldset className="modes">
      <legend>Вариант обработки</legend>
      {modes.map((mode) => {
        const missing = ollamaModels
          ? [mode.pass1, mode.pass2].filter((m, i, all) => all.indexOf(m) === i && !ollamaModels.includes(m))
          : [];
        return (
          <label key={mode.id} className={shown === mode.id ? "mode mode-on" : "mode"}>
            <span className="mode-name">
              <input
                type="radio"
                name={`mode-${file.hash}`}
                checked={shown === mode.id}
                disabled={busy || saving !== null}
                onChange={() => pick(mode.id)}
              />
              {mode.label}
            </span>
            <span className="mode-text">{mode.description}</span>
            {missing.length > 0 && <span className="mode-missing">Не скачана: {missing.join(", ")}</span>}
          </label>
        );
      })}
    </fieldset>
  );
}

type View = "both" | "src" | "dst";
const VIEW_KEY = "reader-view";
const VIEWS: { id: View; label: string; key: string }[] = [
  { id: "src", label: "Оригинал", key: "1" },
  { id: "both", label: "Оба", key: "2" },
  { id: "dst", label: "Перевод", key: "3" },
];

function ViewSwitch({ view, onChange }: { view: View; onChange: (view: View) => void }) {
  return (
    <div className="view-switch" role="group" aria-label="Что показывать">
      {VIEWS.map((v) => (
        <button
          key={v.id}
          type="button"
          className="view-option"
          aria-pressed={view === v.id}
          title={`${v.label} (клавиша ${v.key} в режиме чтения)`}
          onClick={() => onChange(v.id)}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}

export function Reader({ file, modes, ollamaModels, busy, onAction, onRemove, onOpenGlossary }: Props) {
  const [pick, setPick] = useState<Selected | null>(null);
  const [termNote, setTermNote] = useState("");
  const closePick = useMemo(() => (message?: string) => {
    setPick(null);
    window.getSelection()?.removeAllRanges();
    if (message) setTermNote(message);
  }, []);
  const [blocks, setBlocks] = useState<Block[] | null>(null);
  const [error, setError] = useState("");
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [pending, setPending] = useState<number | null>(null);

  // Лента меняется с каждым переведённым куском: по ней текст обновляется без лишних запросов.
  useEffect(() => {
    let cancelled = false;
    api
      .blocks(file.hash)
      .then((data) => {
        if (!cancelled) {
          setBlocks(data.blocks);
          setError("");
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [file.hash, file.strip, file.status]);

  const visible = useMemo(() => {
    if (!blocks || !onlyFlagged) return blocks;
    return blocks.filter((b) => b.parts?.some((p) => ["f", "x", "e", "b"].includes(p.state)));
  }, [blocks, onlyFlagged]);

  useEffect(() => {
    if (pending === null) return;
    scrollToChunk(pending, "center");
    setPending(null);
  }, [pending, visible]);

  const reading = useReadingPosition(file.hash, visible, onlyFlagged);

  // Режим чтения: разворот на весь экран, можно оставить только одну сторону.
  const [view, setView] = useState<View>(() => {
    try { return (localStorage.getItem(VIEW_KEY) as View) || "both"; } catch { return "both"; }
  });
  const [focus, setFocus] = useState(false);
  const [focusNotes, setFocusNotes] = useState(false);
  const changeView = (next: View) => {
    setView(next);
    try { localStorage.setItem(VIEW_KEY, next); } catch { /* без памяти тоже работает */ }
    // Высота абзацев меняется вместе с колонками: возвращаемся к тому же месту.
    if (reading.current !== null) setPending(reading.current);
  };
  const enterFocus = (next?: View) => {
    if (next) changeView(next);
    else if (reading.current !== null) setPending(reading.current);
    setFocus(true);
    document.documentElement.requestFullscreen?.().catch(() => { /* остаёмся в окне */ });
  };
  const exitFocus = () => {
    setFocus(false);
    setFocusNotes(false);
    if (reading.current !== null) setPending(reading.current);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  };
  useEffect(() => {
    if (!focus) return;
    const onFullscreen = () => { if (!document.fullscreenElement) exitFocusRef.current(); };
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as Element)?.closest?.("input, textarea, select");
      if (e.key === "Escape" && !document.fullscreenElement && !document.querySelector(".term-popover")) exitFocusRef.current();
      if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "1") changeViewRef.current("src");
      if (e.key === "2") changeViewRef.current("both");
      if (e.key === "3") changeViewRef.current("dst");
    };
    document.addEventListener("fullscreenchange", onFullscreen);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreen);
      window.removeEventListener("keydown", onKey);
    };
  }, [focus]);
  const exitFocusRef = useRef(exitFocus);
  exitFocusRef.current = exitFocus;
  const changeViewRef = useRef(changeView);
  changeViewRef.current = changeView;

  const [notes, setNotes] = useState<UserNote[]>([]);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesError, setNotesError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api.notes(file.hash)
      .then((data) => { if (!cancelled) setNotes(data.notes); })
      .catch((e: Error) => { if (!cancelled) setNotesError(e.message); });
    return () => { cancelled = true; };
  }, [file.hash]);

  // Номер куска может сдвинуться после повторного разбора книги, а начало абзаца останется прежним.
  const placedNotes = useMemo(() => {
    const parts = (blocks ?? []).flatMap((b) => b.parts ?? []);
    const byIndex = new Map(parts.map((p) => [p.index, p]));
    return notes.map((note) => {
      const same = byIndex.get(note.index);
      if (same && same.src.startsWith(note.quote)) return { note, at: note.index };
      const moved = note.quote ? parts.find((p) => p.src.startsWith(note.quote)) : undefined;
      return { note, at: moved?.index ?? null };
    });
  }, [notes, blocks]);
  const noteAt = useMemo(() => new Map(placedNotes.filter((n) => n.at !== null).map((n) => [n.at as number, n.note])), [placedNotes]);

  const changeNotes = (change: () => Promise<{ notes: UserNote[] }>) => {
    setNotesError("");
    return change()
      .then((data) => { setNotes(data.notes); })
      .catch((e: Error) => setNotesError(`Закладка не сохранилась: ${e.message}`));
  };
  const addBookmark = (part: BlockPart) => changeNotes(() => api.saveNote(file.hash, part.index, "", part.src.slice(0, NOTE_QUOTE_CHARS)));
  const deleteNote = (note: UserNote) => changeNotes(() => api.deleteNote(file.hash, note.index));
  const goToNote = (at: number) => {
    reading.leave();
    setOnlyFlagged(false);
    setPending(at);
  };
  const backToPlace = () => {
    reading.comeBack();
    if (reading.current !== null) setPending(reading.current);
  };

  const jump = (index: number) => {
    setOnlyFlagged(false);
    reading.remember(index);
    setPending(index);
  };

  const notesList = (
    <>
            {notes.length === 0 && <p className="hint">Закладок пока нет. Наведите указатель на абзац и нажмите «Закладка» справа от него.</p>}
      {placedNotes.length > 0 && (
        <ol className="notes-list">
          {placedNotes.map(({ note, at }) => (
            <li key={note.index}>
              {at === null ? (
                <div className="note-lost">
                  <span>Абзац не найден: {note.text || note.quote}</span>
                  <button className="link" onClick={() => deleteNote(note)}>Убрать закладку</button>
                </div>
              ) : (
                <div className="note-item">
                  <button className="note-jump" onClick={() => goToNote(at)}>
                    {note.text && <span className="note-jump-text">{note.text}</span>}
                    <span className="note-jump-quote">{note.quote.slice(0, NOTE_PREVIEW_CHARS) || "Абзац без текста"}</span>
                  </button>
                  <button className="link note-remove" aria-label={`Убрать закладку: ${note.quote.slice(0, 40)}`} onClick={() => deleteNote(note)}>Убрать</button>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </>
  );
  const translated = file.status === "pass2_done" || file.status === "rendered";
  const reviewCount = file.flagged + file.second + file.errors;
  // Пока новый режим не применён, в папке лежит PDF прежнего: кнопка говорит, какой именно.
  const outdatedLabel = file.renderedMode && file.renderedMode !== file.mode
    ? modes.find((m) => m.id === file.renderedMode)?.label.toLowerCase()
    : undefined;

  return (
    <section className={focus ? "book book-focus" : "book"} aria-label={file.name}>
      <header className="book-head">
        <div className="book-title">
          <h1>{file.name.replace(/\.pdf$/i, "")}</h1>
          <p>{statusLine(file, busy)}</p>
        </div>
        <div className="book-actions">
          {file.hasOutput && (
            <>
              <a className="button button-primary" href={`/api/files/${file.hash}/pdf?download=1&v=${file.outputMtime}`} download>
                {outdatedLabel ? `Скачать PDF (${outdatedLabel})` : "Скачать PDF"}
              </a>
              <a className="button" href={`/api/files/${file.hash}/pdf?v=${file.outputMtime}`} target="_blank" rel="noreferrer">
                Открыть
              </a>
            </>
          )}
          <button className="button" disabled={busy} onClick={() => onAction(() => api.translate(file.hash))}>
            {file.status === "rendered" && !file.stale ? "Применить глоссарий" : "Перевести книгу"}
          </button>
          {translated && !file.stale && (
            <button className="button" disabled={busy} onClick={() => onAction(() => api.rerender(file.hash))}>
              Собрать PDF заново
            </button>
          )}
          <button className="button button-quiet" disabled={busy} onClick={onRemove}>
            Убрать из очереди
          </button>
        </div>
      </header>

      <ModePicker file={file} modes={modes} ollamaModels={ollamaModels} busy={busy} onAction={onAction} />

      <div className="ribbon-wrap">
        <Ribbon
          strip={file.strip}
          height={26}
          onPick={jump}
          label={`Состояние книги: переведено ${file.done} из ${file.total}, требуют проверки ${file.flagged}`}
        />
        <div className="ribbon-foot">
          <ul className="legend">
            <li><i className="dot dot-pending" />Ждёт</li>
            <li><i className="dot dot-first" />Первая модель</li>
            <li><i className="dot dot-second" />Исправила вторая</li>
            <li><i className="dot dot-flag" />Нужна проверка</li>
            <li><i className="dot dot-error" />Ошибка</li>
          </ul>
          <button className="link" aria-expanded={notesOpen} onClick={() => setNotesOpen((open) => !open)}>Закладки ({notes.length})</button>
          <button className="link" onClick={onOpenGlossary}>Имена из книги для глоссария</button>
          <span className="view-tools">
            <ViewSwitch view={view} onChange={changeView} />
            <button className="button view-focus" onClick={() => enterFocus()}>Режим чтения</button>
          </span>
          <label className="filter">
            <input
              type="checkbox"
              checked={onlyFlagged}
              disabled={!reviewCount}
              onChange={(e) => {
                setOnlyFlagged(e.target.checked);
                if (!e.target.checked && reading.current !== null) setPending(reading.current);
              }}
            />
            Только с замечаниями и правками ({reviewCount})
          </label>
        </div>
        {notesOpen && (
          <div className="notes-panel">
            {notesList}
          </div>
        )}
      </div>

      {termNote && (
        <p className="banner banner-ok" role="status">
          {termNote}
          <button className="link" onClick={() => setTermNote("")}>Скрыть</button>
        </p>
      )}
      {notesError && <p className="banner banner-error" role="alert">{notesError}</p>}
      {pick && <TermPopover key={`${pick.text}-${pick.x}-${pick.y}`} pick={pick} book={file.hash} onClose={closePick} />}

      <div
        className={`spread view-${view}`}
        ref={reading.spread}
        onClick={(e) => reading.onClick(e.target)}
        onMouseUp={() => setPick((current) => readSelection() ?? (current && document.activeElement?.closest(".term-form") ? current : null))}
        onKeyUp={(e) => { if (e.shiftKey) setPick(readSelection()); }}
        onScroll={() => {
          reading.onScroll();
          setPick((current) => (current && document.activeElement?.closest(".term-form") ? current : null));
        }}
      >
        {focus && (
          <div className="focus-bar">
            <span className="focus-title">{file.name.replace(/\.pdf$/i, "")}</span>
            <ViewSwitch view={view} onChange={changeView} />
            <button className="button view-focus" aria-expanded={focusNotes} onClick={() => setFocusNotes((open) => !open)}>
              Закладки ({notes.length})
            </button>
            <button className="button button-quiet" onClick={exitFocus} title="Выйти (Esc)">Выйти из режима чтения</button>
            {focusNotes && (
              <div className="focus-notes" onClick={(e) => { if ((e.target as Element).closest(".note-jump")) setFocusNotes(false); }}>
                {notesList}
              </div>
            )}
          </div>
        )}
        {error && <p className="spread-empty">Текст не загрузился: {error}</p>}
        {!error && visible && visible.length === 0 && (
          <p className="spread-empty">
            {onlyFlagged ? "Замечаний нет." : "Текст появится здесь после разбора книги. Нажмите «Перевести книгу» или сначала подготовьте глоссарий."}
          </p>
        )}
        {!error && visible && visible.length > 0 && (
          <div className="pages">
            {visible.map((block) => {
              if (block.type === "image") {
                return (
                  <figure className="row row-image" key={block.id}>
                    <img src={`/api/files/${file.hash}/assets/${block.asset?.split("/").pop()}`} alt={`Иллюстрация, страница ${block.page}`} loading="lazy" />
                  </figure>
                );
              }
              if (!block.parts?.length) {
                return (
                  <div className="row" key={block.id}>
                    <div className="side side-src"><p className="para">{block.text}</p></div>
                    <div className="side side-dst"><p className="para">{block.text}</p></div>
                  </div>
                );
              }
              return (
                <div className={block.sectionBreak ? "block block-section" : "block"} key={block.id}>
                  {block.parts.map((part) => (
                    <Part
                      key={part.index}
                      part={part}
                      type={block.type}
                      level={block.level}
                      note={noteAt.get(part.index)}
                      onAdd={addBookmark}
                      onDelete={deleteNote}
                    />
                  ))}
                </div>
              );
            })}
          </div>
        )}
        {reading.away && (
          <div className="return-bar" role="status">
            <span>Место чтения не изменилось. Чтобы продолжить отсюда, щёлкните абзац.</span>
            <button className="button button-primary" onClick={backToPlace}>Вернуться к месту чтения</button>
          </div>
        )}
      </div>
    </section>
  );
}
