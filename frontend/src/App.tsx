import { useCallback, useEffect, useRef, useState } from "react";
import { api, AppState, FileInfo } from "./api";
import { Glossary } from "./Glossary";
import { Reader } from "./Reader";
import { Ribbon } from "./Ribbon";
import { Settings } from "./Settings";

const POLL_MS = 1500;

const STATUS: Record<FileInfo["status"], string> = {
  new: "В очереди",
  extracted: "Переводится",
  pass1_done: "Второй проход",
  pass2_done: "Собирается PDF",
  rendered: "Готово",
};

function fileLabel(file: FileInfo, running: boolean) {
  if (file.errors) return `Ошибок: ${file.errors}`;
  if (file.stale) return "Нужен перевод";
  if (file.status === "extracted" && !running) return file.done ? "Прервано" : "В очереди";
  if (file.status === "pass1_done" && !running) return "Прервано";
  return STATUS[file.status];
}

function useLog() {
  const [lines, setLines] = useState<string[]>([]);
  useEffect(() => {
    const source = new EventSource("/api/events");
    source.addEventListener("reset", () => setLines([]));
    source.onmessage = (event) => setLines((prev) => [...prev.slice(-1999), JSON.parse(event.data)]);
    return () => source.close();
  }, []);
  return lines;
}

function LogPanel({ lines, open, onToggle }: { lines: string[]; open: boolean; onToggle: () => void }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) end.current?.scrollIntoView({ block: "end" });
  }, [lines, open]);
  const last = lines[lines.length - 1] ?? "Перевод ещё не запускался.";
  return (
    <div className={open ? "log log-open" : "log"}>
      <button className="log-bar" onClick={onToggle} aria-expanded={open}>
        <span className="log-title">{open ? "Скрыть журнал" : "Журнал"}</span>
        {!open && <span className="log-last">{last}</span>}
      </button>
      {open && (
        <div className="log-lines" role="log" aria-live="off" tabIndex={0}>
          {lines.map((line, i) => (
            <div key={i} className={line.includes(" ERROR ") ? "log-error" : line.includes(" WARNING ") ? "log-warn" : undefined}>{line}</div>
          ))}
          <div ref={end} />
        </div>
      )}
    </div>
  );
}

export function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [offline, setOffline] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"book" | "settings" | "glossary">("book");
  const [glossaryBook, setGlossaryBook] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [dismissedOutcome, setDismissedOutcome] = useState<number | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const lines = useLog();

  const refresh = useCallback(async () => {
    try {
      setState(await api.state());
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const files = state?.files ?? [];
  const current = files.find((f) => f.hash === selected) ?? files[0] ?? null;
  const running = !!state?.job.running;

  const act = useCallback((action: () => Promise<unknown>) => {
    setNotice("");
    action().catch((e: Error) => setNotice(e.message)).finally(refresh);
  }, [refresh]);

  const upload = async (list: File[]) => {
    if (!list.length) return;
    setUploading(true);
    setNotice("");
    try {
      const result = await api.upload(list);
      if (result.rejected?.length) setNotice(`Не добавлено: ${result.rejected.join("; ")}`);
      await refresh();
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const remove = () => {
    if (!current) return;
    const text = `Убрать «${current.name}» из очереди? Перевод из базы удалится, повторить его можно будет только заново. Готовый PDF в папке out останется.`;
    if (window.confirm(text)) {
      setSelected(null);
      act(() => api.remove(current.hash));
    }
  };

  const unfinished = files.filter((f) => f.status !== "rendered" || f.stale).length;
  const job = state?.job;
  const outcomeKey = job?.outcome ? (job.outcome === "ok" ? null : job.message?.length ?? 0) : null;
  const showOutcome = job && !job.running && (job.outcome === "failed" || job.outcome === "stopped") && dismissedOutcome !== outcomeKey;

  return (
    <div
      className={dragging ? "app app-drag" : "app"}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (e.relatedTarget === null) setDragging(false); }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); upload(Array.from(e.dataTransfer.files)); }}
    >
      <aside className="rail">
        <div className="rail-actions">
          {running ? (
            <button className="button button-stop" onClick={() => act(api.stop)}>Остановить перевод</button>
          ) : (
            <button className="button button-primary" disabled={!state?.ollama.up || !unfinished} onClick={() => act(api.start)}>
              {unfinished ? `Перевести очередь (${unfinished})` : "Очередь переведена"}
            </button>
          )}
          <button className="button" disabled={uploading} onClick={() => picker.current?.click()}>
            {uploading ? "Загружаю" : "Добавить PDF"}
          </button>
          <input
            ref={picker}
            type="file"
            accept=".pdf,application/pdf"
            multiple
            hidden
            onChange={(e) => { upload(Array.from(e.target.files ?? [])); e.target.value = ""; }}
          />
        </div>

        <nav className="queue" aria-label="Очередь книг">
          {files.map((file) => {
            const active = view === "book" && current?.hash === file.hash;
            return (
              <button
                key={file.hash}
                className={active ? "queue-item queue-item-active" : "queue-item"}
                aria-current={active ? "true" : undefined}
                onClick={() => { setSelected(file.hash); setView("book"); }}
              >
                <span className="queue-name">{file.name.replace(/\.pdf$/i, "")}</span>
                <Ribbon strip={file.strip} height={8} label={`Переведено ${file.done} из ${file.total}`} />
                <span className="queue-status">{fileLabel(file, running)}</span>
              </button>
            );
          })}
          {state && !files.length && <p className="queue-empty">Перетащите PDF в окно или нажмите «Добавить PDF».</p>}
        </nav>

        <div className="rail-foot">
          <button className={view === "glossary" ? "link link-active" : "link"} onClick={() => { setGlossaryBook(current?.hash ?? null); setView("glossary"); }}>Глоссарий</button>
          <button className={view === "settings" ? "link link-active" : "link"} onClick={() => setView("settings")}>Варианты обработки</button>
          <p className={state?.ollama.up ? "engine" : "engine engine-down"}>
            {!state ? "Подключаюсь" : state.ollama.up
              ? state.ollama.loaded.length ? `В памяти: ${state.ollama.loaded.join(", ")}` : "Ollama готова"
              : "Ollama не запущена"}
          </p>
        </div>
      </aside>

      <main className="main">
        {offline && <p className="banner banner-error" role="alert">Нет связи с сервером. Проверьте, что translator-ui.exe запущен.</p>}
        {state && !state.ollama.up && <p className="banner banner-error" role="alert">Ollama не отвечает, поэтому перевод недоступен. Запустите приложение Ollama.</p>}
        {notice && <p className="banner banner-error" role="alert">{notice}</p>}
        {showOutcome && (
          <p className={job.outcome === "failed" ? "banner banner-error" : "banner banner-ok"} role="status">
            {job.message || "Задача завершилась с ошибкой. Подробности в журнале."}
            <button className="link" onClick={() => setDismissedOutcome(outcomeKey)}>Скрыть</button>
          </p>
        )}

        {view === "settings" && state && <Settings state={state} onSaved={refresh} />}
        {view === "glossary" && state && <Glossary files={files} initialBook={glossaryBook} jobRunning={Boolean(state.job.running)} />}
        {view === "book" && current && <Reader key={current.hash} file={current} modes={state?.config.modes ?? []} ollamaModels={state?.ollama.up ? state.ollama.models : null} busy={running} onAction={act} onRemove={remove} onOpenGlossary={() => { setGlossaryBook(current.hash); setView("glossary"); }} />}
        {view === "book" && !current && state && (
          <section className="welcome">
            <h1>Положите сюда книгу</h1>
            <p>Перетащите PDF в это окно. Слева появится его лента, а здесь оригинал и перевод на соседних страницах.</p>
            <button className="button button-primary" onClick={() => picker.current?.click()}>Добавить PDF</button>
          </section>
        )}

        <LogPanel lines={lines} open={logOpen} onToggle={() => setLogOpen(!logOpen)} />
      </main>

      {dragging && <div className="drop-veil" aria-hidden="true">Отпустите, чтобы добавить в очередь</div>}
    </div>
  );
}
