"""Клиент Ollama и сборка промпта в формате TranslateGemma.

У модели нет отдельной системной роли (шаблон превращает system в обычный user-ход),
поэтому инструкция, глоссарий и контекст идут одним сообщением перед текстом.
Две пустые строки перед текстом — часть формата модели.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .chunk import estimate_tokens

log = logging.getLogger(__name__)


class OllamaUnavailable(RuntimeError):
    """Сервер недоступен или модель не найдена: продолжать очередь бессмысленно."""


@dataclass
class Result:
    text: str
    truncated: bool
    seconds: float


def load_glossary(path) -> list[tuple[str, str]]:
    path = Path(path)
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 2 and cols[0].strip() and cols[1].strip():
            terms.append((cols[0].strip(), cols[1].strip()))
    return terms


def term_regex(term: str) -> re.Pattern:
    """Начало слова + допускается английское окончание (Hobbit → Hobbits, parry → parried не считается,
    но harass → harassed и abandon → abandoning считаются: лексику в тексте спрягают)."""
    if term[0].isupper():
        return re.compile(r"\b" + re.escape(term) + r"(?:s|es|'s|’s)?\b")
    return re.compile(r"\b" + re.escape(term) + r"(?:s|es|d|ed|ing|'s|’s)?\b", re.IGNORECASE)


def glossary_for(text: str, glossary: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(src, dst) for src, dst in glossary if term_regex(src).search(text)]


def build_prompt(text, source, target, glossary=(), previous=None) -> str:
    sn, sc, tn, tc = source["name"], source["code"], target["name"], target["code"]
    lines = [
        f"You are a professional {sn} ({sc}) to {tn} ({tc}) translator. Your goal is to accurately convey "
        f"the meaning and nuances of the original {sn} text while adhering to {tn} grammar, vocabulary, "
        "and cultural sensitivities.",
    ]
    if glossary:
        lines.append("Use this glossary for names:")
        lines.extend(f"{s} = {d}" for s, d in glossary)
    if previous:
        lines.append("For context, the previous passage was translated as follows (do not translate it again):")
        lines.append(previous)
    # 12B на практике пересчитывала футы в метры с неверными числами.
    lines.append("Keep all numbers and units of measure exactly as in the original; do not convert them.")
    lines.append(
        f"Produce only the {tn} translation, without any additional explanations or commentary. "
        f"Please translate the following {sn} text into {tn}:"
    )
    return "\n".join(lines) + "\n\n\n" + text


_QUOTE_PAIRS = (("«", "»"), ("“", "”"), ('"', '"'))


def clean_output(src: str, dst: str) -> str:
    """Убирает кавычки, в которые модель иногда оборачивает весь ответ. Кавычки из исходника остаются."""
    dst = dst.strip()
    if src.lstrip()[:1] in ('"', "“", "«", "'", "‘"):
        return dst
    for left, right in _QUOTE_PAIRS:
        wrapped = len(dst) > 2 and dst[0] == left and dst[-1] == right
        inner = dst[1:-1]
        if wrapped and left not in inner and right not in inner:
            return inner.strip()
    return dst


class Ollama:
    def __init__(self, cfg: dict):
        self.host = cfg["host"].rstrip("/")
        self.cfg = cfg
        self.loaded: str | None = None

    def check(self, models: list[str]):
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaUnavailable(f"Ollama недоступна по адресу {self.host}: {exc}") from exc
        available = {m["name"] for m in resp.json().get("models", [])}
        missing = [m for m in models if m not in available]
        if missing:
            raise OllamaUnavailable(f"Модели не скачаны: {', '.join(missing)}. Выполните: ollama pull <модель>")

    def unload(self, model: str):
        try:
            requests.post(f"{self.host}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)
        except requests.RequestException as exc:
            log.warning("Не удалось выгрузить %s: %s", model, exc)

    def switch(self, model: str):
        """Перед сменой модели освобождает VRAM (keep_alive: 0)."""
        if self.loaded and self.loaded != model:
            log.info("Выгружаю %s", self.loaded)
            self.unload(self.loaded)
        self.loaded = model

    def translate(self, model: str, prompt: str, src_text: str) -> Result:
        self.switch(model)
        limit = max(self.cfg["min_max_tokens"], int(estimate_tokens(src_text) * self.cfg["max_tokens_ratio"]))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": self.cfg["keep_alive"],
            "options": {
                "temperature": self.cfg["temperature"],
                "num_predict": limit,
                "num_ctx": self.cfg["num_ctx"],
            },
        }
        started = time.perf_counter()
        try:
            resp = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.cfg["request_timeout_s"])
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(f"Соединение с Ollama потеряно: {exc}") from exc
        resp.raise_for_status()
        data = resp.json()
        return Result(
            text=clean_output(src_text, data["message"]["content"]),
            truncated=data.get("done_reason") == "length",
            seconds=time.perf_counter() - started,
        )
