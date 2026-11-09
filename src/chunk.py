"""Блоки IR → чанки: один блок = один чанк, длинные режутся по предложениям."""
from __future__ import annotations

import re

TRANSLATABLE = {"paragraph", "heading", "caption"}
_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "e.g.", "i.e.", "etc.", "fig.", "no.")
_SENTENCE_END = re.compile(r"(?<=[.!?…])([\"'”’)\]]*)\s+(?=[\"'“‘(\[]*[A-ZА-ЯЁ0-9])")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def split_sentences(text: str) -> list[str]:
    out = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end(1)
        last_word = text[start:end].rsplit(None, 1)[-1].lower().lstrip("(\"'“‘")
        if last_word in _ABBREVIATIONS:
            continue
        out.append(text[start:end].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def split_text(text: str, max_tokens: int) -> list[str]:
    """Склеивает предложения в куски не длиннее max_tokens. Сверхдлинное предложение режется по запятым и пробелам."""
    if estimate_tokens(text) <= max_tokens:
        return [text]

    pieces: list[str] = []
    for sentence in split_sentences(text):
        if estimate_tokens(sentence) <= max_tokens:
            pieces.append(sentence)
        else:
            pieces.extend(_split_long(sentence, max_tokens))

    parts: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if current and estimate_tokens(candidate) > max_tokens:
            parts.append(current)
            current = piece
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _split_long(sentence: str, max_tokens: int) -> list[str]:
    limit = max_tokens * 4
    out = []
    rest = sentence
    while len(rest) > limit:
        cut = max(rest.rfind(", ", 0, limit), rest.rfind("; ", 0, limit))
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit - 1
        out.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()
    if rest:
        out.append(rest)
    return out


def needs_translation(block: dict) -> bool:
    if block["type"] not in TRANSLATABLE:
        return False
    return any(ch.isalpha() for ch in block.get("text", ""))


def build_chunks(blocks: list[dict], max_tokens: int) -> list[tuple[int, int, str]]:
    """Возвращает (block_id, part, text)."""
    out = []
    for block in blocks:
        if needs_translation(block):
            for part, text in enumerate(split_text(block["text"], max_tokens)):
                out.append((block["id"], part, text))
    return out
