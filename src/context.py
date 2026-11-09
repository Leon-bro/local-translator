"""Контекст перевода у каждой группы книг свой: глоссарий, подсказки, пол персонажей, слова для обычного перевода.

Группы и принадлежность книг лежат в groups.json, его пишет сервер интерфейса. Группа по умолчанию использует файлы
из config.yaml, остальные лежат в context/<группа>/ с теми же именами файлов.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import characters, llm, names

DEFAULT = "default"
GROUPS_FILE = "groups.json"
CONTEXT_DIR = "context"


@dataclass(frozen=True)
class Context:
    group: str
    glossary: list[tuple[str, str]]
    hints: list[tuple[str, str]]
    plain: list[str]
    decisions: dict[str, str]


def load_groups(root: Path) -> dict:
    """Повреждённый файл это ошибка: иначе книга молча переводилась бы с чужим глоссарием."""
    try:
        data = json.loads((root / GROUPS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"groups": [], "books": {}}
    except ValueError as exc:
        raise SystemExit(f"{GROUPS_FILE} повреждён: {exc}") from exc
    return {"groups": data.get("groups") or [], "books": data.get("books") or {}}


def group_of(groups: dict, fh: str) -> str:
    group = groups["books"].get(fh, DEFAULT)
    if group != DEFAULT and group not in {g["id"] for g in groups["groups"]}:
        raise SystemExit(f"{GROUPS_FILE}: книга {fh} относится к группе {group}, которой нет")
    return group


def files_for(root: Path, paths: dict, group: str) -> dict[str, Path]:
    rel = {"glossary": paths["glossary"], "hints": paths["hints"], "plain": paths["plain_names"],
           "characters": paths.get("characters", "characters.tsv")}
    if group == DEFAULT:
        return {key: root / value for key, value in rel.items()}
    return {key: root / CONTEXT_DIR / group / Path(value).name for key, value in rel.items()}


def load(root: Path, paths: dict, group: str, keep_names: bool) -> Context:
    files = files_for(root, paths, group)
    return Context(
        group=group,
        glossary=llm.load_glossary(files["glossary"]),
        hints=llm.load_glossary(files["hints"]),  # в промпт идут, но проверка их не требует
        plain=names.load_plain(files["plain"]) if keep_names else [],
        decisions=characters.load(files["characters"]),  # решения человека о поле
    )
