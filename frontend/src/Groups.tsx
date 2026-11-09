import { FormEvent, useState } from "react";
import { api, BookGroup, FileInfo } from "./api";

interface Props {
  groups: BookGroup[];
  current: string;
  files: FileInfo[];
  jobRunning: boolean;
  onSelect: (id: string) => void;
  onChanged: (groups: BookGroup[], select?: string) => void;
}

type Editing = { kind: "new" } | { kind: "rename" } | null;

const bookTitle = (file: FileInfo) => file.name.replace(/\.pdf$/i, "");

export function Groups({ groups, current, files, jobRunning, onSelect, onChanged }: Props) {
  const [editing, setEditing] = useState<Editing>(null);
  const [name, setName] = useState("");
  const [copyFrom, setCopyFrom] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const group = groups.find((g) => g.id === current);
  const books = files.filter((f) => group?.books.includes(f.hash));

  const open = (next: Editing) => {
    setEditing(next);
    setName(next?.kind === "rename" ? group?.name ?? "" : "");
    setCopyFrom("");
    setError(""); setMessage("");
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setError(""); setMessage("");
    if (editing?.kind === "new") {
      const before = new Set(groups.map((g) => g.id));
      api.createGroup(name, copyFrom)
        .then((r) => {
          onChanged(r.groups, r.groups.find((g) => !before.has(g.id))?.id);
          setMessage(`Группа «${name.trim()}» создана.`);
          setEditing(null);
        })
        .catch((err: Error) => setError(err.message));
    } else {
      api.renameGroup(current, name)
        .then((r) => { onChanged(r.groups); setEditing(null); })
        .catch((err: Error) => setError(err.message));
    }
  };

  const remove = () => {
    if (!group || !window.confirm(`Удалить группу «${group.name}»? Её глоссарий будет убран из списка, файлы останутся в папке context/_deleted.`)) return;
    setError(""); setMessage("");
    api.deleteGroup(group.id)
      .then((r) => { onChanged(r.groups, "default"); setMessage(`Группа «${group.name}» удалена.`); setEditing(null); })
      .catch((err: Error) => setError(err.message));
  };

  const move = (file: FileInfo, target: string) => {
    setError(""); setMessage("");
    api.setBookGroup(file.hash, target)
      .then((r) => {
        onChanged(r.groups);
        const where = r.groups.find((g) => g.id === target)?.name;
        setMessage(`«${bookTitle(file)}» теперь в группе «${where}».${jobRunning ? " Для идущего перевода это подействует при следующем запуске." : " Чтобы применить к готовой книге, нажмите «Применить глоссарий» на её странице."}`);
      })
      .catch((err: Error) => setError(err.message));
  };

  return (
    <div className="settings-block groups">
      <h2>Группа книг</h2>
      <p className="hint">Глоссарий, подсказки, пол персонажей и обычные слова действуют только внутри группы. Всё на этой странице относится к выбранной группе.</p>
      {error && <p className="banner banner-error" role="alert">{error}</p>}
      {message && <p className="banner banner-ok" role="status">{message}</p>}
      <div className="names-tools">
        <select aria-label="Группа книг" value={current} onChange={(e) => { onSelect(e.target.value); setEditing(null); setError(""); setMessage(""); }}>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}: книг {g.books.length}, терминов {g.terms}</option>)}
        </select>
        <button type="button" className="button" onClick={() => open({ kind: "new" })}>Новая группа</button>
        {current !== "default" && <button type="button" className="button" onClick={() => open({ kind: "rename" })}>Переименовать</button>}
        {current !== "default" && <button type="button" className="button" disabled={books.length > 0} title={books.length > 0 ? "Сначала перенесите книги в другую группу" : undefined} onClick={remove}>Удалить</button>}
      </div>
      {editing && (
        <form className="quick-add" onSubmit={submit}>
          <label className="field"><span>Название группы</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Например, серия книг" autoFocus maxLength={80} />
          </label>
          {editing.kind === "new" && (
            <label className="field"><span>Начальный контекст</span>
              <select value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)}>
                <option value="">Пустой</option>
                {groups.map((g) => <option key={g.id} value={g.id}>Копия: {g.name}</option>)}
              </select>
            </label>
          )}
          <button className="button button-primary" disabled={!name.trim()}>{editing.kind === "new" ? "Создать группу" : "Сохранить название"}</button>
          <button type="button" className="button" onClick={() => setEditing(null)}>Отмена</button>
        </form>
      )}
      {books.length === 0 && <p className="hint">В этой группе пока нет книг. Выберите группу, где лежит нужная книга, и перенесите её сюда.</p>}
      {books.length > 0 && (
        <ul className="group-books">
          {books.map((file) => (
            <li key={file.hash}>
              <span className="group-book-name">{bookTitle(file)}</span>
              <select aria-label={`Перенести в группу: ${bookTitle(file)}`} value={current} onChange={(e) => move(file, e.target.value)}>
                {groups.map((g) => <option key={g.id} value={g.id}>{g.id === current ? g.name : `Перенести: ${g.name}`}</option>)}
              </select>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
