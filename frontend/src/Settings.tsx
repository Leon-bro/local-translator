import { useState } from "react";
import { api, AppState, Mode } from "./api";

interface Props {
  state: AppState;
  onSaved: () => void;
}

function ModelSelect({ label, value, models, onChange, disabled }: {
  label: string;
  value: string;
  models: string[];
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  // Выбранной модели может не быть среди скачанных: она всё равно должна отображаться.
  const options = models.includes(value) ? models : [value, ...models];
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      {options.map((m) => <option key={m} value={m}>{m}</option>)}
    </select>
  );
}

export function Settings({ state, onSaved }: Props) {
  const [modes, setModes] = useState<Mode[]>(state.config.modes);
  const [defaultMode, setDefaultMode] = useState(state.config.defaultMode);
  const [keepNames, setKeepNames] = useState(state.config.keepNames);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const changed = JSON.stringify(modes) !== JSON.stringify(state.config.modes) || defaultMode !== state.config.defaultMode || keepNames !== state.config.keepNames;
  const running = state.job.running;

  const save = async () => {
    setError(""); setMessage("");
    try {
      await api.saveModes(modes, defaultMode, keepNames);
      setMessage("Настройки сохранены. Они применятся к следующему запуску перевода.");
      onSaved();
    } catch (e) { setError((e as Error).message); }
  };

  const updateMode = (id: string, patch: Partial<Mode>) =>
    setModes((list) => list.map((m) => (m.id === id ? { ...m, ...patch } : m)));

  return (
    <section className="settings" aria-label="Варианты обработки">
      <h1>Варианты обработки</h1>
      {message && <p className="banner banner-ok" role="status">{message}</p>}
      {error && <p className="banner banner-error" role="alert">{error}</p>}
      <div className="settings-block">
        <p className="hint">
          Вариант выбирается на странице каждой книги. Первая модель переводит всё, вторая переделывает куски,
          в которых проверка нашла замечания. Одна и та же модель в обоих полях значит, что второго прохода нет.
        </p>
        {!state.ollama.up && <p className="banner banner-error">Ollama не отвечает, список моделей неполный. Запустите Ollama.</p>}
        <table className="mode-table">
          <thead><tr><th scope="col">Вариант</th><th scope="col">Первая модель</th><th scope="col">Вторая модель</th><th scope="col">Для новых книг</th></tr></thead>
          <tbody>
            {modes.map((mode) => (
              <tr key={mode.id}>
                <td>{mode.label}</td>
                <td><ModelSelect label={`Первая модель, ${mode.label}`} value={mode.pass1} models={state.ollama.models} onChange={(v) => updateMode(mode.id, { pass1: v })} disabled={running} /></td>
                <td><ModelSelect label={`Вторая модель, ${mode.label}`} value={mode.pass2} models={state.ollama.models} onChange={(v) => updateMode(mode.id, { pass2: v })} disabled={running} /></td>
                <td><input type="radio" name="default-mode" aria-label={`Для новых книг: ${mode.label}`} checked={defaultMode === mode.id} disabled={running} onChange={() => setDefaultMode(mode.id)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <label className="check-row">
          <input type="checkbox" checked={keepNames} disabled={running} onChange={(e) => setKeepNames(e.target.checked)} />
          <span>Оставлять имена не из глоссария по-английски</span>
        </label>
        <p className="hint">
          Модель не увидит такие имена и названия, поэтому не сможет их исказить. Список можно проверить на странице
          «Глоссарий». Настройка действует на новый перевод: готовые куски не меняются.
        </p>
        <button className="button button-primary mode-save" disabled={!changed || running} onClick={save}>Сохранить настройки</button>
        {running && <p className="hint">Настройки можно менять, когда перевод не идёт.</p>}
      </div>
    </section>
  );
}
