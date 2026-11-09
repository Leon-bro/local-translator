"""Эвристики качества. Возвращают список причин: пустой список означает, что чанк не помечен."""
from __future__ import annotations

import re
from collections import Counter

from .llm import glossary_for, term_regex
from .characters import gender_conflicts, gender_of
from .names import stray_marks

MISSING_TERMS = "нет терминов"
GENDER_CONFLICT = "пол не совпадает"

_LATIN = re.compile(r"[A-Za-z]")
_LETTER = re.compile(r"[^\W\d_]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*")
_NUMBER = re.compile(r"\d+(?:[,.\u00a0]\d+)*")
_IMPERIAL = re.compile(r"\b(foot|feet|inch(es)?|yards?|miles?|leagues?|pounds?|ounces?)\b", re.I)
_METRIC_EN = re.compile(r"\b(\w*met(re|er)s?|\w*gram(me)?s?)\b", re.I)
_METRIC_RU = re.compile(r"\b(кило|санти|милли)?метр|\b(кило)?грамм", re.I)


def _ru_stem(word: str) -> str:
    """Грубая основа с учётом падежей: окончание отбрасывается, короткие слова почти не трогаются."""
    w = word.lower().replace("ё", "е")
    return w[:-2] if len(w) > 3 else w[:-1] if len(w) == 3 else w


# Беглая гласная: «уровень» → «уровня», «боец» → «бойца», «замок» → «замка».
_FLEETING = re.compile(r"(ень|ец|ок|ек)$")


def _ru_stems(word: str) -> tuple[str, ...]:
    w = word.lower().replace("ё", "е")
    stem = _ru_stem(w)
    return (stem, w[:-3]) if len(w) >= 6 and _FLEETING.search(w) else (stem,)


def _parts(text: str) -> list[str]:
    """Слова, а слова через дефис по частям: «демона-пэра» склоняется в обеих частях, «Д-Диабло» это заикание."""
    return [p for w in _WORD.findall(text) for p in re.split(r"[-'’]", w) if p]


def term_present(translation: str, dst: str) -> bool:
    """Слова перевода термина стоят в тексте подряд и в том же порядке (в любом падеже).
    Иначе «Мертвый День рождения» сошёл бы за «Рождение мёртвых»."""
    words = [w.lower().replace("ё", "е") for w in _parts(translation)]
    stems = [_ru_stems(term) for term in _parts(dst)]
    return any(all(any(words[i + j].startswith(s) for s in options) for j, options in enumerate(stems))
               for i in range(len(words) - len(stems) + 1))


def required_terms(src: str, glossary) -> list[tuple[str, str]]:
    """Термины исходника, перевод которых должен быть в тексте. Короткий термин внутри длинного не требуется:
    в «Diable Chevaliers» достаточно «диабль-шевалье», отдельного «шевалье» там нет."""
    found = glossary_for(src, glossary)
    rest, keep = src, set()
    for s, d in sorted(found, key=lambda t: -len(t[0])):
        pattern = term_regex(s)
        if pattern.search(rest):
            keep.add((s, d))
            rest = pattern.sub(lambda m: " " * len(m.group()), rest)
    return [t for t in found if t in keep]


def latin_share(dst: str, glossary) -> float:
    """Доля латинских букв в переводе, не считая слов из терминов глоссария."""
    letters = _LETTER.findall(dst)
    if not letters:
        return 0.0
    allowed = {w.lower() for term, _ in glossary for w in _LATIN_WORD.findall(term)}
    latin = sum(
        len(_LATIN.findall(word))
        for word in _LATIN_WORD.findall(dst)
        if word.lower() not in allowed
    )
    return latin / len(letters)


def max_ngram_repeats(text: str, n: int) -> int:
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < n:
        return 0
    counts = Counter(tuple(words[i : i + n]) for i in range(len(words) - n + 1))
    return max(counts.values())


def missing_numbers(src: str, dst: str) -> list[str]:
    """Числа исходника, которых нет в переводе. Разделители разрядов не учитываются."""
    normalize = lambda text: {re.sub(r"[\s,.\u00a0]", "", n) for n in _NUMBER.findall(text)}
    return sorted(normalize(src) - normalize(dst))


def units_converted(src: str, dst: str) -> bool:
    """В исходнике имперские единицы без метрических, а в переводе появились метрические."""
    return bool(_IMPERIAL.search(src) and _METRIC_RU.search(dst) and not _METRIC_EN.search(src))


def fixable_by_terms(reasons: str | None) -> bool:
    """Такие замечания может снять новый глоссарий или указанный пол."""
    return MISSING_TERMS in (reasons or "") or GENDER_CONFLICT in (reasons or "")


def check(src: str, dst: str | None, truncated: bool, glossary, cfg: dict, plain=(), genders=None) -> list[str]:
    if not dst or not dst.strip():
        return ["пустой перевод"]

    reasons = []
    if truncated:
        reasons.append("перевод обрезан: не хватило лимита длины")

    if len(src) >= cfg["short_block_chars"]:
        ratio = len(dst) / len(src)
        if not cfg["min_length_ratio"] <= ratio <= cfg["max_length_ratio"]:
            reasons.append(f"отношение длин {ratio:.2f}")

    share = latin_share(dst, glossary)
    if share > cfg["max_latin_share"]:
        reasons.append(f"латиница {share:.0%}")

    missing = [s for s, d in required_terms(src, glossary) if not term_present(dst, d)]
    # Слово просили переводить, а в переводе осталась латиница: кусок переведён, пока оно считалось именем.
    missing += [n for n in plain if term_regex(n).search(src) and term_regex(n).search(dst)]
    if missing:
        reasons.append(f"{MISSING_TERMS}: " + ", ".join(missing))

    if genders:
        people = [(s, d, gender_of(s, genders)) for s, d in glossary_for(src, glossary) if s[:1].isupper()]
        wrong = gender_conflicts(dst, [p for p in people if p[2]])
        if wrong:
            reasons.append(f"{GENDER_CONFLICT}: " + ", ".join(wrong))

    stray = stray_marks(src, dst)
    if stray:
        reasons.append("лишние метки: " + ", ".join(stray))

    numbers = missing_numbers(src, dst)
    if numbers:
        reasons.append("нет чисел: " + ", ".join(numbers))

    if units_converted(src, dst):
        reasons.append("единицы пересчитаны")

    repeats = max_ngram_repeats(dst, cfg["ngram_size"])
    if repeats > cfg["ngram_max_repeats"] and repeats > max_ngram_repeats(src, cfg["ngram_size"]):
        reasons.append(f"повтор {cfg['ngram_size']}-граммы ×{repeats}")

    return reasons
