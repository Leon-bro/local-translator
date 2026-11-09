"""Импорт глоссария из .xlsx.

Строки таблиц для людей бывают трёх видов, и обращаться с ними нужно по-разному:
- точные термины («Marco = Марко») идут в glossary.tsv: модель обязана их использовать, проверка это требует;
- советы с вариантами («parry = парировать, отбить удар») идут в hints.tsv: модель их видит, проверка не требует,
  иначе каждый удачный синоним отправлял бы кусок на второй проход;
- описания вместо перевода («Tsk = цоканье языком») не импортируются: модель вставила бы их в текст.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

SOURCE_HEADERS = ("english", "source", "term", "оригинал", "термин", "англ")
TARGET_HEADERS = ("русск", "перевод", "russian", "target", "translation")
CATEGORY_HEADERS = ("категор", "category", "раздел")
SKIP_CATEGORIES = ("звук", "междомет", "sound", "interject", "onomatop")
ONE_S_FORMS = ("his", "her", "their", "my", "your", "our", "its")
MIN_SOURCE_CHARS = 2


@dataclass
class ImportResult:
    terms: list[tuple[str, str]] = field(default_factory=list)
    hints: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _split(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s+/\s+|,\s+", text) if p.strip()]


def expand(source: str, target: str) -> list[tuple[str, str]]:
    """Одна строка таблицы → пары. «Ciel, Raphael» с равным числом частей делится попарно."""
    source = _clean(source).rstrip("….").strip()
    target = _clean(target).strip("«»")

    pair = re.match(r"^(.+?)\s*\((.+)\)$", source)
    if pair:
        outer, inner = pair.groups()
        translated = re.match(r"^(.+?)\s*\((.+)\)$", target)
        if translated:  # Gazel (King Gazel) = Газель (король Газель)
            return expand(outer, translated.group(1)) + expand(inner, translated.group(2))
        if inner[0].isupper():  # Oboro (Crestwater) = Оборо: второе написание того же
            return expand(outer, target) + expand(inner, target)
        return expand(outer, target)  # dismiss (words): уточнение, а не вариант
    source = re.sub(r"\s*\([^)]*\)\s*", " ", source).strip()  # go (be) on the move

    sources, targets = _split(source), _split(target)
    if len(sources) > 1 and len(sources) == len(targets):
        return [p for s, t in zip(sources, targets) for p in expand(s, t)]
    if len(sources) > 1:
        return []  # неоднозначно, какое слово чему соответствует

    if len(source) < MIN_SOURCE_CHARS or not target:
        return []
    if "one's" in source or "one’s" in source:
        return [(re.sub(r"one['’]s", form, source), target) for form in ONE_S_FORMS]
    return [(source, target)]


def is_strict(source: str, target: str) -> bool:
    """Только однозначное имя собственное можно требовать дословно."""
    if not source[0].isupper() or re.search(r"[,(…]", target):
        return False
    # Unique = уникальный: обычное слово, у которого заглавная может быть из-за начала предложения.
    return not (" " not in source and " " not in target and target[0].islower())


def _columns(header: tuple) -> tuple[int, int, int | None] | None:
    names = [_clean(c).lower() for c in header]
    find = lambda options: next((i for i, n in enumerate(names) if any(o in n for o in options)), None)
    source, target = find(SOURCE_HEADERS), find(TARGET_HEADERS)
    if source is None or target is None or source == target:
        return None
    return source, target, find(CATEGORY_HEADERS)


def read_workbook(path) -> ImportResult:
    result = ImportResult()
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            columns = _columns(rows[0])
            if columns:
                rows = rows[1:]
            elif sheet.max_column and sheet.max_column >= 2:
                columns = (0, 1, None)  # таблица без заголовка
            else:
                continue
            source_col, target_col, category_col = columns
            for row in rows:
                source = _clean(row[source_col]) if source_col < len(row) else ""
                target = _clean(row[target_col]) if target_col < len(row) else ""
                if not source or not target:
                    continue
                category = _clean(row[category_col]).lower() if category_col is not None and category_col < len(row) else ""
                if any(word in category for word in SKIP_CATEGORIES):
                    result.skipped.append(source)
                    continue
                pairs = expand(source, target)
                if not pairs:
                    result.skipped.append(source)
                for s, t in pairs:
                    (result.terms if is_strict(s, t) else result.hints).append((s, t))
    finally:
        workbook.close()
    return result


def _read_tsv(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    comments, pairs = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                comments.append(line)
            elif "\t" in line:
                cols = line.split("\t")
                pairs.append((cols[0].strip(), cols[1].strip()))
    return comments, pairs


def _merge(path: Path, incoming: list[tuple[str, str]], drop: set[str] = frozenset()) -> tuple[int, int]:
    comments, current = _read_tsv(path)
    current = [(s, t) for s, t in current if s not in drop]
    index = {s: i for i, (s, _) in enumerate(current)}
    added = updated = 0
    for source, target in incoming:
        if source in index:
            if current[index[source]][1] != target:
                current[index[source]] = (source, target)
                updated += 1
        else:
            index[source] = len(current)
            current.append((source, target))
            added += 1
    lines = comments + [f"{s}\t{t}" for s, t in current]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return added, updated


def import_file(path, glossary_path, hints_path) -> dict:
    """Дополняет глоссарий и подсказки. Повторный импорт того же файла ничего не меняет."""
    data = read_workbook(path)
    strict = {s for s, _ in data.terms}
    added, updated = _merge(Path(glossary_path), data.terms)
    hints_added, hints_updated = _merge(Path(hints_path), [(s, t) for s, t in data.hints if s not in strict], strict)
    return {"added": added, "updated": updated, "hintsAdded": hints_added, "hintsUpdated": hints_updated,
            "skipped": data.skipped}
