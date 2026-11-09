"""Какие имена книги принадлежат персонажам и какого они пола.

Это эвристика по английскому тексту, без модели: разбор тома занимает секунды и не занимает видеопамять.
Пол выводится только из надёжных признаков: обращение («Lady Shuna») и местоимение в авторской речи сразу после
имени в роли подлежащего. Реплики в кавычках не считаются: в них говорят о других людях. Если признаков мало или они
спорят, пол остаётся пустым, его заполняет человек. Выбор человека лежит в файле и всегда важнее вывода.
"""
from __future__ import annotations

import re

MALE, FEMALE, NOT_PERSON = "m", "f", "x"
VALUES = (MALE, FEMALE, NOT_PERSON)

_MALE_TITLES = {"mr", "sir", "lord", "king", "prince", "emperor", "master", "mister", "duke", "baron", "count", "father", "brother", "uncle", "grandfather"}
_FEMALE_TITLES = {"mrs", "ms", "miss", "lady", "queen", "princess", "empress", "madam", "mistress", "duchess", "baroness", "countess", "mother", "sister", "aunt", "grandmother"}
_NEUTRAL_TITLES = {"dr", "general", "major", "colonel", "captain", "lieutenant", "commander", "sergeant", "marshal", "minister", "professor", "chef", "saint", "demon"}
_TITLES = _MALE_TITLES | _FEMALE_TITLES | _NEUTRAL_TITLES
# Родство и «Master» часто стоят перед названием, а не только перед именем, поэтому слабее.
_WEAK_TITLES = {"master", "father", "mother", "brother", "sister", "count", "major"}

_MALE_NOUNS = {"man", "boy", "guy", "lad", "gentleman", "son", "brother", "father", "king", "prince", "emperor", "husband", "uncle"}
_FEMALE_NOUNS = {"woman", "girl", "lady", "maiden", "daughter", "sister", "mother", "queen", "princess", "empress", "wife", "maid", "witch", "aunt"}
_APPOSITION = re.compile(r"^,?\s+(?:the|a|an|that|this)\s+(?:[a-z\-]+\s+){0,2}?([a-z\-]+)\b")

_SPEECH = (r"said|asked|replied|answered|shouted|yelled|whispered|muttered|murmured|exclaimed|continued|added|explained"
           r"|cried|called|laughed|sighed|groaned|grumbled|snapped|nodded|smiled|grinned|frowned|shrugged|chuckled|smirked")
_ACTS = (r"looked|thought|knew|felt|wanted|decided|realized|noticed|stood|sat|walked|ran|turned|stared|glared|watched"
         r"|told|spoke|bowed|hesitated|wondered|remembered|believed|understood|agreed|admitted|seemed|tried|hoped|refused")
_SAYS_AFTER = re.compile(rf"^\s+(?:[a-z]+ly\s+)?(?:{_SPEECH}|{_ACTS})\b")
_SAYS_BEFORE = re.compile(rf"[”\"]\s*(?:{_SPEECH})\s+$")
# После имени идёт сказуемое: иначе местоимение чаще относится к другому человеку.
_SUBJECT = re.compile(r"^\s+(?:[a-z]+ly\s+)?(?:was|is|were|had|has|did|could|would|should|might|must|will|can|[a-z]{2,}ed|[a-z]{3,}s)\b(?!\s+of\b)")
_NOT_VERB = {"this", "his", "always", "perhaps", "across", "towards", "besides", "as", "us", "thus", "is"}
# «at», «from» и «toward» здесь нет: «looked at Yuuki» говорит о человеке.
_PLACE_BEFORE = re.compile(r"\b(?:the|in|inside|within|throughout)\s+$", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?…]+(?:[.!?…]+[”\"’]*|$)")
_QUOTE = re.compile(r"“[^”]*(?:”|$)|\"[^\"]*(?:\"|$)")
_HE = re.compile(r"\b(?:he|him|his|himself)\b", re.IGNORECASE)
_SHE = re.compile(r"\b(?:she|her|hers|herself)\b", re.IGNORECASE)
_PRONOUN = re.compile(r"\b(?:he|him|his|himself|she|her|hers|herself)\b", re.IGNORECASE)
_CAPITAL = re.compile(r"\b[A-Z][a-zA-Z\-]+")

TITLE_WEIGHT = 4
WEAK_TITLE_WEIGHT = 1
APPOSITION_WEIGHT = 3
MIN_EVIDENCE = 3
MIN_SHARE = 0.8
MIN_PERSON_SIGNALS = 2
MAX_EVIDENCE_QUOTES = 2
QUOTE_CHARS = 170


def load(path) -> dict[str, str]:
    """Решения человека: имя → m, f или x."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for line in lines:
        cols = line.split("\t")
        if line.startswith("#") or len(cols) < 2:
            continue
        name, value = cols[0].strip(), cols[1].strip()
        if name and value in VALUES:
            out[name] = value
    return out


def base_name(name: str) -> str:
    """«Sir Rimuru» и «Lady Shuna» это те же Rimuru и Shuna."""
    words = name.split()
    while len(words) > 1 and words[0].lower().rstrip(".") in _TITLES:
        words = words[1:]
    return " ".join(words)


def _may_be_name(name: str) -> bool:
    """«Demon Lord» после снятия обращений становится просто «Lord», а «of Justice» обрывок названия."""
    base = base_name(name)
    return base[:1].isupper() and base.lower() not in _TITLES


def _narration(text: str) -> str:
    """Убирает реплики, сохраняя длину: позиции в тексте остаются верными."""
    return _QUOTE.sub(lambda m: " " * len(m.group()), text)


def _sentences(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _SENTENCE.finditer(text)]


class _Tally:
    __slots__ = ("he", "she", "human", "place", "seen", "male_title", "female_title", "quotes")

    def __init__(self):
        self.he = self.she = self.human = self.place = self.seen = 0
        self.male_title = self.female_title = 0
        self.quotes: dict[str, list[str]] = {MALE: [], FEMALE: [], "": []}

    def quote(self, key: str, sentence: str):
        bucket = self.quotes[key]
        sentence = " ".join(sentence.split())
        if len(bucket) < MAX_EVIDENCE_QUOTES and sentence not in bucket:
            bucket.append(sentence[:QUOTE_CHARS] + ("…" if len(sentence) > QUOTE_CHARS else ""))


def _name_pattern(names) -> re.Pattern:
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z\-])(" + alternatives + r")(['’]s)?(?![A-Za-z\-])")


def _is_subject(after: str) -> bool:
    match = _SUBJECT.match(after)
    return bool(match) and match.group().split()[-1].lower() not in _NOT_VERB


def _title_before(before: str) -> str | None:
    match = re.search(r"([A-Z][a-z]+)\.?\s+$", before)
    return match.group(1).lower() if match else None


def _around(text: str, match: re.Match, reach: int) -> str:
    """Отрывок вокруг имени: края обрезаны по границе слова."""
    left, right = max(0, match.start() - reach), min(len(text), match.end() + reach)
    if left:
        left = text.find(" ", left, match.start()) + 1 or match.start()
    if right < len(text):
        right = text.rfind(" ", match.end(), right)
        right = right if right > 0 else match.end()
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def _read_block(text: str, pattern: re.Pattern, tally: dict[str, _Tally], names: set[str]):
    plain = _narration(text)
    spans = _sentences(plain)
    # Все упоминания: и в репликах, там работают обращения («Lady Shuna»).
    for match in pattern.finditer(text):
        name = base_name(match.group(1))
        record = tally[name]
        record.seen += 1
        before, after = text[:match.start()], text[match.end():]
        title = _title_before(before)
        if title in _TITLES:
            record.human += 1
            weight = WEAK_TITLE_WEIGHT if title in _WEAK_TITLES else TITLE_WEIGHT
            if title in _MALE_TITLES:
                record.male_title += weight
                record.quote(MALE, _around(text, match, 60))
            elif title in _FEMALE_TITLES:
                record.female_title += weight
                record.quote(FEMALE, _around(text, match, 60))
        if plain[match.start()] == " ":  # внутри реплики местоимения и сказуемые не читаем
            continue
        if _PLACE_BEFORE.search(before):
            record.place += 1
        possessive = bool(match.group(2))
        if not possessive and (_SAYS_AFTER.match(after) or _SAYS_BEFORE.search(before)):
            record.human += 1
            record.quote("", _around(text, match, 80))
        if not possessive:
            apposition = _APPOSITION.match(after)
            if apposition:
                noun = apposition.group(1)
                if noun in _MALE_NOUNS:
                    record.male_title += APPOSITION_WEIGHT
                    record.human += 1
                elif noun in _FEMALE_NOUNS:
                    record.female_title += APPOSITION_WEIGHT
                    record.human += 1
        if possessive or not _is_subject(after):
            continue
        _read_pronoun(plain, spans, match, pattern, record)


def _read_pronoun(plain, spans, match, pattern, record):
    """Первое местоимение после подлежащего: в том же предложении или в начале следующего."""
    here = next((i for i, (a, b) in enumerate(spans) if a <= match.start() < b), None)
    if here is None:
        return
    stop = spans[here][1]
    window = plain[match.end():stop]
    if here + 1 < len(spans):
        start, end = spans[here + 1]
        following = plain[start:end]
        first = following.lstrip().split(" ", 1)[0].lower().strip(",.;")
        # Следующее предложение, начатое местоимением, продолжает говорить о том же подлежащем.
        if first in ("he", "she", "his", "her") and not _PRONOUN.search(window):
            window = following
    found = _PRONOUN.search(window)
    if not found or _other_name_between(window[:found.start()], pattern):
        return
    if _HE.fullmatch(found.group()):
        record.he += 1
        record.human += 1
        record.quote(MALE, plain[spans[here][0]:stop])
    else:
        record.she += 1
        record.human += 1
        record.quote(FEMALE, plain[spans[here][0]:stop])


def _other_name_between(segment: str, pattern: re.Pattern) -> bool:
    return bool(pattern.search(segment) or _CAPITAL.search(segment))


def _decide(record: _Tally) -> tuple[str, bool]:
    """(пол или пусто, уверенно ли это человек)."""
    male, female = record.he + record.male_title, record.she + record.female_title
    total = male + female
    gender = ""
    if total >= MIN_EVIDENCE:
        if male / total >= MIN_SHARE:
            gender = MALE
        elif female / total >= MIN_SHARE:
            gender = FEMALE
    person = record.human >= MIN_PERSON_SIGNALS and record.human > record.place
    return gender, person


def analyze(texts, names) -> dict[str, dict]:
    """Основа имени → человек ли, вероятный пол и на чём этот вывод основан."""
    bases = {base_name(n) for n in names if n and _may_be_name(n)}
    if not bases:
        return {}
    # Ищем без обращений: «Sir» перед найденным Rimuru и есть признак пола.
    pattern = _name_pattern(bases)
    tally = {b: _Tally() for b in bases}
    for text in texts:
        _read_block(text, pattern, tally, bases)
    out = {}
    for name, record in tally.items():
        gender, person = _decide(record)
        evidence = record.quotes[gender] if gender else record.quotes[""] + record.quotes[MALE] + record.quotes[FEMALE]
        out[name] = {"person": person, "gender": gender, "he": record.he + record.male_title,
                     "she": record.she + record.female_title, "count": record.seen,
                     "evidence": evidence[:MAX_EVIDENCE_QUOTES]}
    return out


def _owner(name: str, singles: set[str]) -> str | None:
    """«Rimuru Tempest» это Rimuru, если такое короткое имя тоже есть в книге."""
    words = name.split()
    if len(words) < 2:
        return None
    return words[0] if words[0] in singles else None


def report(texts, names, decisions: dict[str, str]) -> list[dict]:
    """Строки для экрана: один человек занимает одну строку, частые выше, неуверенные в конце."""
    found = analyze(texts, names)
    singles = {n for n in found if " " not in n}
    rows: dict[str, dict] = {}
    for name in sorted(found, key=len):
        owner = _owner(name, singles)
        if owner:
            rows[owner]["count"] += found[name]["count"]
            rows[owner]["aliases"].append(name)
            continue
        info = found[name]
        rows[name] = {"term": name, "aliases": [], "person": info["person"], "guess": info["gender"],
                      "he": info["he"], "she": info["she"], "count": info["count"], "evidence": info["evidence"],
                      "choice": decisions.get(name, "")}
    # Выбор человека перекрывает вывод, в том числе про то, персонаж ли это.
    for row in rows.values():
        if row["choice"] in (MALE, FEMALE):
            row["person"] = True
        elif row["choice"] == NOT_PERSON:
            row["person"] = False
    out = [r for r in rows.values() if r["count"] > 0]
    out.sort(key=lambda r: (-r["count"], r["term"]))
    return out


def gender_of(name: str, genders: dict[str, str]) -> str:
    """Пол для имени и его длинных форм: у «Sir Rimuru» и «Rimuru Tempest» он тот же, что у Rimuru."""
    base = base_name(name)
    return genders.get(base.split()[0]) or genders.get(base) or genders.get(name, "")


GENDER_LABEL = {MALE: "male, он", FEMALE: "female, она"}  # «он» чуть лучше держит мужской род имён на -а


def annotate(entries, tokens: dict[str, str], genders: dict[str, str]) -> list[tuple[str, str]]:
    """Строки глоссария для промпта с полом: «Shuna = Шуна (female, она)», для скрытого имени «ZXQ1 = ZXQ1 (female, она)»."""
    out = []
    for number, surface in tokens.items():
        gender = gender_of(surface, genders)
        if gender:
            mark = f"ZXQ{number}"
            out.append((mark, f"{mark} ({GENDER_LABEL[gender]})"))
    for source, target in entries:
        gender = gender_of(source, genders) if source[:1].isupper() else ""
        out.append((source, f"{target} ({GENDER_LABEL[gender]})" if gender else target))
    return out


def effective(texts, names, decisions: dict[str, str]) -> dict[str, str]:
    """Пол, с которым идёт перевод: решение человека, иначе уверенный вывод для уверенного персонажа."""
    out = {}
    for name, info in analyze(texts, names).items():
        if info["person"] and info["gender"]:
            out[name] = info["gender"]
    for name, value in decisions.items():
        if value == NOT_PERSON:
            out.pop(name, None)
        else:
            out[name] = value
    return out


# Глагол прошедшего времени выдаёт род. Существительные на те же буквы встречаются после имени редко, частые исключены.
_NOT_VERBS = frozenset("""генерал маршал капитал зал вал бал финал сигнал канал материал идеал метал
сила стрела дела тела стекла зла игла пчела мгла""".split())
_MASCULINE = r"[а-яё]*[аеёиоуыюя]л(?:ся)?"
_FEMININE = r"[а-яё]*ла(?:сь)?"
_BOUND_L, _BOUND_R = r"(?<![а-яёА-ЯЁA-Za-z\-])", r"(?![а-яёА-ЯЁA-Za-z\-])"


def gender_conflicts(dst: str, people) -> list[str]:
    """Имена, рядом с которыми глагол другого рода: «Шион кивнул» при женском поле.

    Смотрим только на глагол сразу после имени и в ремарке («— сказал Шион»). Имя берём в начальной форме: в косвенном
    падеже оно не подлежащее. Несогласованность дальше по предложению эта проверка не найдёт.
    """
    wrong = []
    for name, target, gender in people:
        verb = _MASCULINE if gender == FEMALE else _FEMININE
        forms = {re.escape(name), re.escape(target)}
        alternatives = "|".join(sorted(forms, key=len, reverse=True))
        who = rf"{_BOUND_L}(?:{alternatives}){_BOUND_R}"
        after = rf"{who}\s+(?:[а-яё]+о\s+)?(?:не\s+)?({verb}){_BOUND_R}"
        before = rf"[—–,\-]\s+({verb})\s+{who}"
        hits = re.findall(after, dst) + re.findall(before, dst)
        if any(hit.lower() not in _NOT_VERBS for hit in hits):
            wrong.append(name)
    return wrong
