"""PDF → промежуточное представление (blocks.json + assets/).

Абзацы определяются по геометрии строк, а не по блокам PyMuPDF: в книжной вёрстке
один блок часто покрывает целую страницу, а признак абзаца — отступ первой строки.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

# Увеличивать при изменении результата извлечения: книги разберутся заново,
# а перевод повторится только для кусков, текст которых действительно изменился.
EXTRACT_VERSION = 3  # 3: картинки, выходящие за край страницы, и размер картинок

_PAGE_WORD = r"(?:p\s*a\s*g\s*e|pg\.?|pp?\.|с\s*т\s*р(?:аница)?\.?)"
_DECOR = r"[\s|·•:.\-–—/\\]*"
# Подпись страницы занимает всю строку: «Page | 70», «Page 70 of 300», «70 | P a g e», «Стр. 70».
PAGE_LABEL_RE = re.compile(
    rf"^{_DECOR}(?:{_PAGE_WORD}{_DECOR}\d+(?:\s*(?:of|из|/)\s*\d+)?|\d+{_DECOR}{_PAGE_WORD}){_DECOR}$",
    re.IGNORECASE,
)
LABEL_ZONE_RATIO = 0.15    # одиночная подпись считается колонтитулом только у края страницы
STABLE_ZONE_RATIO = 0.25   # где искать подписи с номером на одной высоте
STABLE_Y_TOLERANCE = 3.0

HYPHENS = ("-", "­", "‐", "‑")
TERMINAL = ".!?…:;\"'”’»)]"
BULLET_RE = re.compile(r"^([•·▪◦‣*–—-]|\d{1,3}[.)])\s")
CAPTION_RE = re.compile(r"^(fig(ure)?\.?|table|plate|map|illustration|рис\.?|рисунок|таблица)\s*\d*", re.I)
# Без TEXT_PRESERVE_LIGATURES: «ﬁ» раскрывается в «fi», иначе слова ломаются в промпте.
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_IMAGES
SENTENCE_END_RE = re.compile(r"[.!?…:;][\"'”»’)\]]*\s*$")
FULL_PAGE_SHARE = 0.8  # картинка занимает почти всю страницу: в переводе она тоже идёт отдельной страницей


class ExtractError(RuntimeError):
    pass


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    text: str
    page_height: float
    left: float = 0.0
    right: float = 0.0


@dataclass
class Image:
    page: int
    bbox: tuple
    data: bytes
    ext: str
    y0: float
    full: bool = False  # иллюстрация на всю страницу


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def _merge_visual_lines(raw_lines: list[dict]) -> list[list[dict]]:
    """Склеивает фрагменты одной визуальной строки (капитель и разрядка дают их несколько)."""
    groups: list[list[dict]] = []
    for line in raw_lines:
        y0, y1 = line["bbox"][1], line["bbox"][3]
        if groups:
            py0 = min(l["bbox"][1] for l in groups[-1])
            py1 = max(l["bbox"][3] for l in groups[-1])
            overlap = min(y1, py1) - max(y0, py0)
            if overlap > 0.5 * min(y1 - y0, py1 - py0):
                groups[-1].append(line)
                continue
        groups.append([line])
    return groups


def _read_page(page: pymupdf.Page, min_image_pt: float) -> list:
    items: list = []
    height = page.rect.height
    area = page.rect.width * page.rect.height
    # Без clip PyMuPDF пропускает картинки, хоть немного выходящие за край: так теряются иллюстрации в обрез.
    for block in page.get_text("dict", flags=TEXT_FLAGS, clip=pymupdf.INFINITE_RECT())["blocks"]:
        visible = pymupdf.Rect(block["bbox"]) & page.rect
        if visible.is_empty:
            continue
        if block["type"] == 1:
            x0, y0, x1, y1 = visible
            if min(x1 - x0, y1 - y0) >= min_image_pt:
                full = visible.width * visible.height >= FULL_PAGE_SHARE * area
                items.append(Image(page.number + 1, tuple(visible), block["image"], block["ext"], y0, full))
            continue
        for group in _merge_visual_lines(block["lines"]):
            group.sort(key=lambda l: l["bbox"][0])
            spans = [s for l in group for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = _clean(" ".join("".join(s["text"] for s in l["spans"]) for l in group))
            if not text:
                continue
            weights = Counter()
            for s in spans:
                weights[round(s["size"], 1)] += len(s["text"])
            bold_chars = sum(
                len(s["text"]) for s in spans if s["flags"] & 16 or "bold" in s["font"].lower()
            )
            items.append(Line(
                page=page.number + 1,
                x0=min(l["bbox"][0] for l in group),
                y0=min(l["bbox"][1] for l in group),
                x1=max(l["bbox"][2] for l in group),
                y1=max(l["bbox"][3] for l in group),
                size=weights.most_common(1)[0][0],
                bold=bold_chars > 0.7 * sum(len(s["text"]) for s in spans),
                text=text,
                page_height=height,
            ))
    return _place_images(items)


def _place_images(items: list) -> list:
    """PyMuPDF отдаёт картинки после всего текста страницы. Ставим каждую перед первой строкой ниже неё:
    иначе разделитель сцены посреди страницы оказался бы в конце и разорвал абзац, идущий на следующую."""
    text = [it for it in items if not isinstance(it, Image)]
    for image in sorted((it for it in items if isinstance(it, Image)), key=lambda it: it.y0):
        at = next((i for i, it in enumerate(text) if not isinstance(it, Image) and it.y0 > image.y0), len(text))
        text.insert(at, image)
    return text


def _in_margin(y0: float, y1: float, height: float, ratio: float) -> bool:
    return y1 <= height * ratio or y0 >= height * (1 - ratio)


def _furniture_key(text: str) -> str:
    return re.sub(r"[\d\W_]+", " ", text.lower()).strip()


def _stable_numbered_lines(pages: list[list]) -> set[int]:
    """Строки с номером, стоящие на одной высоте на многих страницах: «Seite 12» или «12» у края,
    даже если они чуть выходят за зону полей. id() строк, которые нужно убрать."""
    groups: dict[str, list[Line]] = {}
    for items in pages:
        for it in items:
            if (isinstance(it, Line) and len(it.text) <= 80 and re.search(r"\d", it.text)
                    and _in_margin(it.y0, it.y1, it.page_height, STABLE_ZONE_RATIO)):
                groups.setdefault(_furniture_key(it.text), []).append(it)

    threshold = max(3, math.ceil(len(pages) * 0.3))
    found: set[int] = set()
    for lines in groups.values():
        for line in lines:
            near = {o.page for o in lines if abs(o.y0 - line.y0) <= STABLE_Y_TOLERANCE}
            if len(near) >= threshold:
                found.add(id(line))
    return found


def _page_labels(pages: list[list]) -> set[int]:
    """Подписи вида «Page | 70». Из-за порядка строк в PDF они вклеиваются в середину предложения,
    поэтому ищутся по всей странице. Подпись посреди страницы должна повторяться в документе:
    одиночная строка «Page 5» там может быть настоящим текстом."""
    labels = [it for items in pages for it in items if isinstance(it, Line) and PAGE_LABEL_RE.match(it.text)]
    repeated = len({line.page for line in labels}) >= 2
    return {id(line) for line in labels
            if repeated or _in_margin(line.y0, line.y1, line.page_height, LABEL_ZONE_RATIO)}


def strip_furniture(pages: list[list], margin_ratio: float) -> tuple[list[list], int]:
    """Убирает колонтитулы, номера страниц и повторяющиеся картинки на полях."""
    always = _page_labels(pages) | _stable_numbered_lines(pages)
    counts: Counter = Counter()
    image_counts: Counter = Counter()
    for items in pages:
        seen = set()
        seen_images = set()
        for it in items:
            if isinstance(it, Line) and _in_margin(it.y0, it.y1, it.page_height, margin_ratio):
                seen.add(_furniture_key(it.text))
            elif isinstance(it, Image):
                seen_images.add(hash(it.data))
        counts.update(seen)
        image_counts.update(seen_images)

    threshold = max(2, math.ceil(len(pages) * 0.1))
    removed = 0
    result = []
    for items in pages:
        kept = []
        for it in items:
            if id(it) in always:
                removed += 1
                continue
            if isinstance(it, Line) and _in_margin(it.y0, it.y1, it.page_height, margin_ratio):
                key = _furniture_key(it.text)
                if not key or counts[key] >= threshold:
                    removed += 1
                    continue
            elif isinstance(it, Image) and image_counts[hash(it.data)] >= threshold:
                removed += 1
                continue
            kept.append(it)
        result.append(kept)
    return result, removed


def _join_hyphenated(left: str, right: str, known: set[str]) -> str:
    """Склейка строк: перенос убирается, если только слово не пишется через дефис в самом документе."""
    if left and left[-1] in HYPHENS and right and right[0].islower():
        stem = left[:-1]
        head = stem.rsplit(" ", 1)[-1].lower()
        tail = re.match(r"[^\W\d_]+", right)
        if left[-1] == "-" and tail and f"{head}-{tail.group(0).lower()}" in known:
            return left + right
        return stem + right
    if left and left[-1] in HYPHENS[2:] or (left and left[-1] == "­"):
        return left[:-1] + right
    return f"{left} {right}"


def _body_metrics(lines: list[Line]) -> tuple[float, float, bool]:
    """Основной кегль, типичный шаг строк и признак вёрстки с красной строкой."""
    weights: Counter = Counter()
    for line in lines:
        weights[line.size] += len(line.text)
    body_size = weights.most_common(1)[0][0]

    gaps = []
    for a, b in zip(lines, lines[1:]):
        if a.page == b.page and a.size == b.size == body_size and 0 < b.y0 - a.y0 < body_size * 3:
            gaps.append(b.y0 - a.y0)
    line_step = statistics.median(gaps) if gaps else body_size * 1.2

    indented = sum(
        1 for l in lines
        if l.size == body_size and body_size * 0.6 < l.x0 - l.left < body_size * 5
    )
    body_lines = sum(1 for l in lines if l.size == body_size)
    # В обычном абзаце 5–15 строк: это 7–20 % строк с отступом.
    return body_size, line_step, body_lines > 0 and 0.03 < indented / body_lines < 0.4


def _assign_flows(lines: list[Line]):
    """Задаёт границы потока текста каждой строке. Поток заканчивается на смене страницы или
    при возврате вверх (новая колонка), поэтому отступы в колонках считаются от своей границы."""
    flows: list[list[Line]] = []
    for line in lines:
        if flows and line.page == flows[-1][-1].page and line.y0 >= flows[-1][-1].y0 - line.size:
            flows[-1].append(line)
        else:
            flows.append([line])
    for flow in flows:
        left = Counter(round(l.x0) for l in flow).most_common(1)[0][0]
        right = max(l.x1 for l in flow)
        for line in flow:
            line.left, line.right = left, right


def _is_heading(line: Line, body_size: float, body_is_bold: bool) -> bool:
    if line.size >= body_size * 1.15:
        return True
    if line.size < body_size * 0.95 or len(line.text) > 120 or line.text[-1] in ".,;:":
        return False
    return line.bold and not body_is_bold


def build_blocks(pages: list[list]) -> list[dict]:
    lines = [it for items in pages for it in items if isinstance(it, Line)]
    if not lines:
        return []

    _assign_flows(lines)
    body_size, line_step, indent_style = _body_metrics(lines)
    body_is_bold = Counter(l.bold for l in lines if l.size == body_size).most_common(1)[0][0]
    heading_sizes = sorted(
        {l.size for l in lines if _is_heading(l, body_size, body_is_bold)}, reverse=True
    )
    known = {w.lower() for l in lines for w in re.findall(r"[^\W\d_]+-[^\W\d_]+", l.text[:-1])}

    blocks: list[dict] = []
    open_block: dict | None = None
    prev: Line | None = None
    after_image = False
    deferred: list[dict] = []  # картинки, которые ждут конца абзаца

    def close():
        nonlocal open_block
        if open_block:
            blocks.append(open_block)
        open_block = None
        blocks.extend(deferred)
        deferred.clear()

    def start(line: Line, kind: str, section_break=False):
        nonlocal open_block
        close()
        open_block = {"type": kind, "page": line.page, "text": line.text}
        if section_break:
            open_block["section_break"] = True
        if kind == "heading":
            open_block["level"] = min(heading_sizes.index(line.size) + 1, 3) if line.size in heading_sizes else 3

    for items in pages:
        for it in items:
            if isinstance(it, Image):
                x0, y0, x1, y1 = it.bbox
                image = {"type": "image", "page": it.page, "data": it.data, "ext": it.ext,
                         "width_pt": round(x1 - x0, 1), "height_pt": round(y1 - y0, 1)}
                if it.full:
                    image["full_page"] = True
                # Иллюстрация между страницами посреди абзаца: абзац не рвём, картинка встаёт после него.
                if it.full and open_block and open_block["type"] == "paragraph" and not SENTENCE_END_RE.search(open_block["text"]):
                    deferred.append(image)
                    continue
                close()
                blocks.append(image)
                prev = None
                after_image = True
                continue

            line = it
            heading = _is_heading(line, body_size, body_is_bold)
            kind = "heading" if heading else "paragraph"
            if after_image and not heading and (CAPTION_RE.match(line.text) or line.size < body_size * 0.95):
                kind = "caption"
            after_image = False

            if open_block is None or prev is None:
                start(line, kind)
                prev = line
                continue

            indent = line.x0 - line.left
            is_indented = line.size * 0.6 < indent < line.size * 5
            new_flow = line.page != prev.page or line.y0 < prev.y0 - line.size

            if heading or open_block["type"] != kind:
                same_heading = (
                    heading and open_block["type"] == "heading"
                    and line.size == prev.size and not new_flow
                    and line.y0 - prev.y0 < max(line.size, prev.size) * 2
                )
                if same_heading:
                    open_block["text"] = _join_hyphenated(open_block["text"], line.text, known)
                else:
                    start(line, kind)
            elif new_flow:
                # Новая страница или колонка: абзац продолжается, если нет признаков нового.
                ended = prev.text[-1] in TERMINAL
                continues = (
                    not is_indented and (indent_style or not ended) and abs(line.size - prev.size) < 0.8
                    and not BULLET_RE.match(line.text)
                )
                if continues:
                    open_block["text"] = _join_hyphenated(open_block["text"], line.text, known)
                else:
                    start(line, kind)
            else:
                gap = line.y0 - prev.y0
                prev_indent = prev.x0 - prev.left
                prev_short = (
                    prev.x1 < prev.right - (prev.right - prev.left) * 0.12
                    and prev.text[-1] in TERMINAL
                )
                large_gap = gap > line_step * 1.4
                indent_break = is_indented and not (line.size * 0.6 < prev_indent < line.size * 5)
                size_break = abs(line.size - prev.size) >= 0.8
                # При красной строке короткая строка без отступа — просто реплика или стих внутри абзаца.
                short_break = prev_short and not indent_style
                if large_gap or indent_break or size_break or short_break or BULLET_RE.match(line.text):
                    start(line, kind, section_break=large_gap and indent_style)
                else:
                    open_block["text"] = _join_hyphenated(open_block["text"], line.text, known)
            prev = line
    close()
    return blocks


def extract(pdf_path, work_dir, cfg: dict) -> list[dict]:
    """Создаёт work_dir/blocks.json и work_dir/assets/, возвращает блоки."""
    work_dir = Path(work_dir)
    assets = work_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise ExtractError(f"не удалось открыть PDF: {exc}") from exc
    if doc.needs_pass:
        raise ExtractError("PDF защищён паролем")

    with doc:
        page_size = (doc[0].rect.width, doc[0].rect.height) if len(doc) else (595, 842)
        pages = [_read_page(page, cfg["min_image_pt"]) for page in doc]

    pages, removed = strip_furniture(pages, cfg["margin_ratio"])
    blocks = build_blocks(pages)

    counters: Counter = Counter()
    for i, block in enumerate(blocks):
        block["id"] = i
        if block["type"] == "image":
            counters[block["page"]] += 1
            name = f"p{block['page']}_img{counters[block['page']]}.{block['ext']}"
            (assets / name).write_bytes(block.pop("data"))
            block.pop("ext")
            block["asset"] = f"assets/{name}"

    text_chars = sum(len(b.get("text", "")) for b in blocks)
    if text_chars < 20 * max(1, len(pages)):
        log.warning("%s: почти нет текстового слоя (%d симв.), похоже на скан. OCR в v1 не поддерживается.",
                    Path(pdf_path).name, text_chars)
    log.info("%s: страниц %d, блоков %d, убрано колонтитулов %d", Path(pdf_path).name, len(pages), len(blocks), removed)

    payload = {"extract_version": EXTRACT_VERSION, "page_size": page_size, "blocks": blocks}
    (work_dir / "blocks.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return blocks


def load_blocks(work_dir) -> dict:
    return json.loads((Path(work_dir) / "blocks.json").read_text(encoding="utf-8"))
