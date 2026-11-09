import type { Candidate, Character, CharacterChoice, Term } from "./api";

/** Украшения из ответа чат-модели: нумерация, маркеры списка, жирный шрифт, код. */
function cleanCell(text: string): string {
  return text.trim().replace(/^["'`*_]+|["'`*_]+$/g, "").trim();
}

function fromTableRow(text: string): Term | null | undefined {
  if (!text.startsWith("|")) return undefined;
  const cells = text.replace(/^\||\|$/g, "").split("|").map(cleanCell);
  if (cells.length < 2 || cells.every((c) => /^:?-+:?$/.test(c) || !c)) return null;
  if (/^(english|original|term|source|оригинал|термин|английск)/i.test(cells[0])) return null;
  return cells[0] && cells[1] ? { source: cells[0], target: cells[1] } : null;
}

/** Строка списка: «Hobbit = хоббит», а также табуляция, «;», стрелка, тире в пробелах, запятая (CSV) или таблица. */
export function parseTermLine(line: string): Term | null {
  const text = line.trim().replace(/^(?:\d+[.)]\s+|[-*•]\s+)/, "");
  if (!text || text.startsWith("#") || text.startsWith("```") || text.endsWith(":")) return null;
  const row = fromTableRow(text);
  if (row !== undefined) return row;
  for (const separator of ["\t", "=", ";", "→", "->", " — ", " – ", " - ", ","]) {
    const at = text.indexOf(separator);
    if (at > 0) {
      const source = cleanCell(text.slice(0, at));
      const target = cleanCell(text.slice(at + separator.length).replace(/\s(?:#|\/\/)\s.*$/, ""));
      if (source && target) return { source, target };
    }
  }
  return null;
}

/** Строка без перевода («Adapter =») и служебная разметка ошибкой не считаются. */
function isIgnorable(line: string): boolean {
  const text = line.trim();
  return !text || text.startsWith("#") || text.startsWith("```") || /^\|[\s:|-]*\|$/.test(text) || /[^=]=\s*$/.test(text) || text.endsWith(":");
}

export function parseList(text: string) {
  const terms: Term[] = [];
  const failed: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    const term = parseTermLine(line);
    // Оригинал без латиницы это реплика чата («Если нужно, могу уточнить»), а не термин.
    if (term && /[A-Za-z]/.test(term.source)) terms.push(term);
    else if (!isIgnorable(line) && !line.trim().startsWith("|")) failed.push(line.trim());
  }
  return { terms, failed };
}

/** Запрос для другой модели: читается как задание, а после заполнения загружается обратно как список. */
export function buildNamesRequest(book: string, candidates: Candidate[], known: Term[]): string {
  const lines = [
    `# Это имена и термины из книги «${book}». Переведи их с английского на русский для глоссария художественного перевода.`,
    "#",
    "# Правила:",
    "# 1. Верни только строки вида «оригинал = перевод», по одной на термин, без пояснений.",
    "# 2. Оригинал копируй в точности, не меняй написание и порядок слов.",
    "# 3. Перевод давай в именительном падеже и начальной форме. Для одного термина один вариант, без скобок и синонимов.",
    "# 4. Если у серии есть устоявшийся перевод, используй его. Имена передавай транскрипцией, говорящие названия переводи.",
    "# 5. Если термин лучше оставить по-английски, повтори его справа без изменений: «Adapter = Adapter».",
    "# 6. Пример в строке над термином показывает, как он употреблён в книге. Сами примеры переводить не нужно.",
  ];
  if (known.length) {
    lines.push("#", "# Уже принятые переводы из этой книги. Придерживайся их написания:");
    lines.push(...known.map((t) => `# ${t.source} = ${t.target}`));
  }
  lines.push("#", "# Термины:");
  for (const c of candidates) {
    lines.push(`# ${c.count} раз: ${c.example.replace(/\s+/g, " ")}`, `${c.term} =`);
  }
  return lines.join("\n") + "\n";
}

/** Раскладывает ответ модели: имена из таблицы книги и всё остальное. */
export function matchAnswer(terms: Term[], candidates: Candidate[]) {
  const known = new Set(candidates.map((c) => c.term));
  const matched: Term[] = [];
  const other: Term[] = [];
  for (const term of terms) {
    (known.has(term.source) ? matched : other).push(term);
  }
  return { matched, other };
}

/** Запрос для другой модели: пол персонажей, в том же виде, что и список имён. */
export function buildCharactersRequest(book: string, characters: Character[]): string {
  const lines = [
    `# Это имена из книги «${book}». Для каждого укажи пол персонажа: от этого зависит форма глагола в русском переводе.`,
    "#",
    "# Правила:",
    "# 1. Верни только строки вида «имя = значение», по одной на имя, без пояснений.",
    "# 2. Имя копируй в точности, не меняй написание и порядок слов.",
    "# 3. Значение одно из трёх: м (мужской), ж (женский), нет (не персонаж: навык, место, организация, предмет).",
    "# 4. Если у персонажа нет пола, выбирай род, которым о нём обычно говорят в русском переводе серии.",
    "# 5. Если не знаешь ответа, оставь имя без значения: «Vega =». Не угадывай.",
    "# 6. Строки над именем показывают, как оно употреблено в книге. Переводить их не нужно.",
    "#",
    "# Имена:",
  ];
  for (const c of characters) {
    const quotes = c.evidence.length ? c.evidence : [];
    lines.push(`# ${c.count} раз${c.aliases.length ? `, также ${c.aliases.join(", ")}` : ""}`);
    lines.push(...quotes.map((q) => `# ${q.replace(/\s+/g, " ")}`));
    lines.push(`${c.term} =`);
  }
  return lines.join("\n") + "\n";
}

/** Значение из ответа модели. null: понять нельзя, такую строку человек решит сам. */
export function parseGender(text: string): Exclude<CharacterChoice, ""> | null {
  const value = text.trim().toLowerCase().replace(/[.!()«»"]/g, "").trim();
  if (/^(м|муж|мужской|мужской род|мужчина|он|m|male|man|he)$/.test(value)) return "m";
  if (/^(ж|жен|женский|женский род|женщина|она|f|female|woman|she)$/.test(value)) return "f";
  if (/^(нет|не персонаж|не человек|x|х|none|no|not a person|not a character|навык|место|организация|предмет)$/.test(value)) return "x";
  return null;
}

/** Раскладывает ответ модели по строкам таблицы. Длинная форма имени относится к строке персонажа. */
export function matchGenders(terms: Term[], characters: Character[]) {
  const owners = new Map<string, string>();
  for (const c of characters) {
    owners.set(c.term, c.term);
    for (const alias of c.aliases) owners.set(alias, c.term);
  }
  const matched: Record<string, Exclude<CharacterChoice, "">> = {};
  const unclear: string[] = [];
  const other: string[] = [];
  for (const { source, target } of terms) {
    const owner = owners.get(source);
    const gender = parseGender(target);
    if (!owner) other.push(source);
    else if (!gender) unclear.push(`${source} = ${target}`);
    else matched[owner] = gender;
  }
  return { matched, unclear, other };
}
