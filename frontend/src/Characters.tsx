import { useEffect, useMemo, useRef, useState } from "react";
import { api, Character, CharacterChoice } from "./api";
import { buildCharactersRequest, matchGenders, parseList } from "./termList";

type Tab = "fill" | "known" | "other";
type Filled = Exclude<CharacterChoice, "">;

const GENDER: Record<"m" | "f", string> = { m: "мужской", f: "женский" };
const TAB_NAME: Record<Tab, string> = { fill: "Нужно заполнить", known: "Определены", other: "Не похожи на людей" };

/** Строка остаётся на своей вкладке, пока список не загрузят заново: иначе она исчезала бы из-под курсора. */
function tabOf(c: Character): Tab {
  if (c.choice === "x" || (!c.choice && !c.person)) return "other";
  return c.choice || c.guess ? "known" : "fill";
}

interface RowProps {
  character: Character;
  draft?: Filled;
  onChange: (value: CharacterChoice) => void;
}

function GenderRow({ character: c, draft, onChange }: RowProps) {
  return (
    <tr className={draft ? "character-draft" : undefined}>
      <td className="name-term">
        {c.term}
        {c.aliases.length > 0 && <div className="character-aliases">также {c.aliases.join(", ")}</div>}
      </td>
      <td>{c.count}</td>
      <td>
        <select aria-label={`Пол: ${c.term}`} value={draft ?? c.choice} onChange={(e) => onChange(e.target.value as CharacterChoice)}>
          <option value="">{c.guess ? `По тексту: ${GENDER[c.guess]}` : "Не указан"}</option>
          <option value="m">Мужской</option>
          <option value="f">Женский</option>
          <option value="x">Не персонаж</option>
        </select>
        {draft && <span className="character-draft-note">Из ответа, ещё не сохранено</span>}
      </td>
      <td className="name-example">
        {c.evidence.map((line) => <p className="character-evidence" key={line}>{line}</p>)}
        {c.he + c.she > 0 && <span>Признаков: «он» {c.he}, «она» {c.she}</span>}
      </td>
    </tr>
  );
}

export function Characters({ characters, bookName, group }: { characters: Character[] | null; bookName: string; group: string }) {
  const [rows, setRows] = useState<Character[]>([]);
  const [tabs, setTabs] = useState<Record<string, Tab>>({});
  const [tab, setTab] = useState<Tab>("fill");
  const [drafts, setDrafts] = useState<Record<string, Filled>>({});
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const answerFile = useRef<HTMLInputElement>(null);
  const parsedAnswer = useMemo(() => parseList(answer), [answer]);

  useEffect(() => {
    const list = characters ?? [];
    setRows(list);
    setTabs(Object.fromEntries(list.map((c) => [c.term, tabOf(c)])));
    setTab(list.some((c) => tabOf(c) === "fill") ? "fill" : "known");
    setDrafts({});
    setMessage("");
  }, [characters]);

  const counts = useMemo(() => {
    const out: Record<Tab, number> = { fill: 0, known: 0, other: 0 };
    for (const c of rows) out[tabs[c.term] ?? tabOf(c)]++;
    return out;
  }, [rows, tabs]);
  const shown = rows.filter((c) => (tabs[c.term] ?? tabOf(c)) === tab);
  const draftCount = Object.keys(drafts).length;

  const save = async (items: { term: string; value: CharacterChoice }[]) => {
    setError(""); setMessage("");
    try {
      await api.saveCharacters(items, group);
    } catch (e) {
      setError(`Выбор не сохранился: ${(e as Error).message}`);
      return false;
    }
    const values = new Map(items.map((i) => [i.term, i.value]));
    setRows((current) => current.map((c) => (values.has(c.term) ? { ...c, choice: values.get(c.term)! } : c)));
    setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([term]) => !values.has(term))));
    setMessage("Сохранено. Новый выбор действует при следующем запуске перевода. Чтобы исправить готовую книгу, нажмите «Применить глоссарий» на её странице.");
    return true;
  };

  const saveDrafts = () => save(Object.entries(drafts).map(([term, value]) => ({ term, value })));

  const request = () => buildCharactersRequest(bookName, shown);

  const downloadRequest = () => {
    const url = URL.createObjectURL(new Blob([request()], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${bookName} - пол персонажей.txt`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const copyRequest = () => {
    setError(""); setMessage("");
    navigator.clipboard.writeText(request())
      .then(() => setMessage("Запрос скопирован. Вставьте его в чат другой модели, а её ответ верните сюда."))
      .catch((e: Error) => setError(`Не удалось скопировать: ${e.message}`));
  };

  /** Ответ попадает в таблицу на проверку, сохраняет его кнопка «Сохранить заполненные». */
  const applyAnswer = () => {
    const { matched, unclear, other } = matchGenders(parsedAnswer.terms, rows);
    const filled = Object.keys(matched).length;
    setDrafts((current) => ({ ...current, ...matched }));
    const parts = [`Заполнено строк в таблице: ${filled}. Проверьте их и нажмите «Сохранить заполненные».`];
    if (unclear.length) parts.push(`Не понял значение в ${unclear.length}: ${unclear.slice(0, 3).join("; ")}.`);
    if (other.length) parts.push(`Имён не из книги: ${other.length}, они пропущены.`);
    setError(""); setMessage(parts.join(" "));
    setAnswer(parsedAnswer.failed.join("\n"));
  };

  const tabButton = (id: Tab) => (
    <button type="button" className="button" aria-pressed={tab === id} onClick={() => setTab(id)}>{TAB_NAME[id]} ({counts[id]})</button>
  );

  return (
    <div className="settings-block">
      <h2>Персонажи и пол</h2>
      <p className="hint">
        Пол помогает модели выбрать форму глагола: «кивнул» или «кивнула». Определённый по тексту пол применяется сам,
        ваш выбор всегда важнее. Там, где текста не хватило, укажите пол вручную или спросите другую модель.
      </p>
      {characters === null && <p className="hint">Ищу персонажей в книге.</p>}
      {error && <p className="banner banner-error" role="alert">{error}</p>}
      {message && <p className="banner banner-ok" role="status">{message}</p>}
      {characters !== null && rows.length === 0 && <p className="hint">Персонажей в этой книге не нашлось.</p>}
      {rows.length > 0 && (
        <>
          <div className="character-tabs">
            {tabButton("fill")}
            {tabButton("known")}
            {tabButton("other")}
          </div>
          <div className="names-tools">
            <button type="button" className="button button-primary" disabled={!draftCount} onClick={saveDrafts}>
              Сохранить заполненные{draftCount ? ` (${draftCount})` : ""}
            </button>
          </div>
          {shown.length === 0 && <p className="hint">{tab === "fill" ? "Заполнять нечего: пол всех найденных персонажей известен." : "Здесь пусто."}</p>}
          {shown.length > 0 && (
            <table className="names characters">
              <thead><tr><th scope="col">Имя</th><th scope="col">Раз</th><th scope="col">Пол</th><th scope="col">Как это видно в тексте</th></tr></thead>
              <tbody>
                {shown.map((c) => <GenderRow key={c.term} character={c} draft={drafts[c.term]} onChange={(value) => save([{ term: c.term, value }])} />)}
              </tbody>
            </table>
          )}
          <div className="names-exchange">
            <h3>Заполнить другой моделью</h3>
            <p className="hint">
              В список попадёт открытая вкладка: «{TAB_NAME[tab]}», {shown.length}. Скачайте его или скопируйте запрос и отправьте
              другой модели. Её ответ вставьте сюда или загрузите файлом: значения появятся в таблице, и вы сможете их проверить
              перед сохранением.
            </p>
            <div className="names-tools">
              <button type="button" className="button" disabled={!shown.length} onClick={downloadRequest}>Скачать список</button>
              <button type="button" className="button" disabled={!shown.length} onClick={copyRequest}>Скопировать запрос</button>
            </div>
            <textarea
              className="term-list"
              aria-label="Ответ другой модели о поле персонажей"
              rows={5}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              placeholder={"Ciel = ж\nVega = м\nStorm Blast = нет"}
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
                aria-label="Файл с ответом другой модели о поле персонажей"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) file.text().then(setAnswer).catch((err: Error) => setError(err.message));
                  e.target.value = "";
                }}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
