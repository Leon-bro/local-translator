"""Имена и особые термины, которых нет в глоссарии: в переводе они остаются по-английски.

TranslateGemma игнорирует просьбу не переводить слово: и прямая инструкция, и строка «Adapter = Adapter»
дают «Адаптер». Поэтому модель вообще не видит такие слова: перед переводом они заменяются метками ZXQ1, ZXQ2,
а после перевода возвращаются. Потерянную метку находит проверка, как и потерянный термин.

Формат метки и отсутствие инструкции о ней подобраны на реальном тексте. Метки [1], @1 и <x1/> модели теряют,
а просьба «метки означают имена» заставляет 12B заменять метками другие имена. ZXQ1 похоже на обычный код:
его копируют как есть (на 10 кусках обе модели не потеряли ни одного из 14 имён).
"""
from __future__ import annotations

import re

_NAME = re.compile(r"[A-Z][A-Za-z’'\-]*(?:\s+[A-Z][A-Za-z’'\-]*)*")
_WORD = re.compile(r"[A-Za-z][A-Za-z’'\-]*")
_POSSESSIVE = re.compile(r"['’]s?$")
_MARK = re.compile(r"ZXQ(\d+)", re.IGNORECASE)
_OPENERS = "\"'“”‘’«(—–-["
_ENDINGS = ".!?…:;"
# Слова, которые пишутся с заглавной, но имеют обычный перевод.
COMMON = frozenset("""
I I'm I’m I'd I’d I'll I’ll I've I’ve OK Ok Oh Ah Mr Mrs Ms Dr Sir Lady Lord Master Miss Madam Saint
King Queen Prince Princess Emperor Empress General Major Colonel Captain Lieutenant Commander Sergeant
God Gods Heaven Hell Earth Sun Moon North South East West Northern Southern Eastern Western
River Sea Ocean Lake Mountain Mountains Hill Hills Forest Valley Island City Town
One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Hundred Thousand First Second Third
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December English Japanese Russian French German Chinese Volume Chapter Part Page
Majesty Highness Excellency Honor Grace Marshal Chef Yen Birthday Bloody Artist Mister Father Mother Brother Sister
""".split())
_SOUNDS = frozenset("""
ah aah oh ooh eh heh hee keh kwah gwah ha haha hah hmm hmmm hm hmph huh uh um umm uh-huh mm mmm ngh tsk tch ugh
ugh gah whoa wow yeah yep nope ow ouch phew psst shh wah waah yikes ee eek
""".split())
MAX_WORDS = 4
MAX_CANDIDATES = 500
EXAMPLE_CONTEXT = 45
_HEADING_MAX_WORDS = 12
_HEADING_CAPITALS = 0.6


def load_plain(path) -> list[str]:
    """Слова, которые пользователь попросил переводить обычным образом."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _sentence_start(text: str, at: int) -> bool:
    before = text[:at].rstrip().rstrip(_OPENERS).rstrip()
    return not before or before[-1] in _ENDINGS


def _is_heading(text: str) -> bool:
    words = _WORD.findall(text)
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    return sum(w[0].isupper() for w in words) / len(words) > _HEADING_CAPITALS


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _is_sound(word: str) -> bool:
    """Междометие или заикание: их переводят или передают по-русски, а не оставляют латиницей."""
    low = word.lower()
    parts = [p for p in low.split("-") if p]
    return (low in _SOUNDS or parts[0] in _SOUNDS or len(set(parts)) < len(parts)
            or (len(parts) > 1 and parts[1].startswith(parts[0]))
            or not re.search(r"[aeiouy]", low))


def _occurrences(texts, known, plain):
    """(название, текст, позиция) для каждого упоминания в середине предложения."""
    lower = {w.lower() for text in texts for w in _WORD.findall(text) if w[0].islower()}
    stop = {w.lower() for term, _ in known for w in _WORD.findall(term)}
    stop |= {w.lower() for term in plain for w in _WORD.findall(term)}

    def excluded_word(word: str) -> bool:
        low = word.lower()
        return word in COMMON or _singular(word) in COMMON or low in stop or _singular(low) in stop

    def excluded(word: str) -> bool:
        pieces = [p for p in word.split("-") if p]
        return excluded_word(word) or _is_sound(word) or all(excluded_word(p) for p in pieces)

    def everyday(word: str) -> bool:
        pieces = [p.lower() for p in word.split("-") if p]
        return all(p in lower or _singular(p) in lower or p.capitalize() in COMMON for p in pieces)

    for text in texts:
        if _is_heading(text):
            continue
        for match in _NAME.finditer(text):
            words = match.group().split()
            if _sentence_start(text, match.start()):
                words = words[1:]  # заглавная первого слова ничего не значит: «The Emperor Rudra» → Emperor Rudra
            part: list[str] = []
            # Исключённое слово разрывает название: «Emperor Rudra» → Rudra.
            for word in [*map(lambda w: _POSSESSIVE.sub("", w), words), None]:
                if word and not excluded(word):
                    part.append(word)
                    continue
                name = " ".join(part)
                # Несколько слов с заглавной подряд это название, даже если каждое по отдельности обычное:
                # «Probability Manipulation». Одно такое слово может быть просто выделением.
                single_everyday = len(part) == 1 and everyday(part[0])
                if len(name) >= 2 and len(part) <= MAX_WORDS and not name.isupper() and not single_everyday:
                    yield name, text, match.start()
                part = []


def find_names(texts, known=(), plain=()) -> list[str]:
    """Имя: слово с заглавной в середине предложения, которое нигде в книге не пишется со строчной."""
    return sorted({name for name, _, _ in _occurrences(texts, known, plain)})


def candidates(texts, known=(), plain=()) -> list[dict]:
    """Имена книги для интерфейса: сколько раз встречаются и пример употребления."""
    examples: dict[str, tuple[str, int]] = {}
    for name, text, at in _occurrences(texts, known, plain):
        examples.setdefault(name, (text, at))
    out = []
    for name, (text, at) in examples.items():
        left, right = max(0, at - EXAMPLE_CONTEXT), min(len(text), at + len(name) + EXAMPLE_CONTEXT)
        example = ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        out.append({"term": name, "count": sum(len(pattern.findall(t)) for t in texts), "example": example})
    out.sort(key=lambda c: (-c["count"], c["term"]))
    return out[:MAX_CANDIDATES]


def mask(text: str, names) -> tuple[str, dict[str, str]]:
    """Заменяет имена метками. Окончание притяжательного падежа остаётся: модели нужна связь слов."""
    if not names:
        return text, {}
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pattern = re.compile(r"\b(?:" + alternatives + r")(?:s|es)?\b")
    tokens: dict[str, str] = {}
    by_name: dict[str, str] = {}
    counter = 0

    def replace(match: re.Match) -> str:
        nonlocal counter
        surface = match.group()
        if surface not in by_name:
            counter += 1
            by_name[surface] = str(counter)
            tokens[str(counter)] = surface
        return f"ZXQ{by_name[surface]}"

    return pattern.sub(replace, text), tokens


def restore(text: str, tokens: dict[str, str]) -> str:
    return _MARK.sub(lambda m: tokens.get(m.group(1), m.group()), text)


def stray_marks(src: str, dst: str) -> list[str]:
    """Метки в переводе, которых не было в исходнике: модель их выдумала или исказила."""
    return sorted(set(_MARK.findall(dst)) - set(_MARK.findall(src)))
