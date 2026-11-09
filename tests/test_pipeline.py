"""Тесты без Ollama и браузера: синтетические PDF и поддельный LLM-клиент."""
import json
import shutil
import sys
from pathlib import Path

import pymupdf
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import characters, chunk, cli, context, extract, glossary_import, llm, names, render, validate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXTRACT_CFG = {"margin_ratio": 0.09, "min_image_pt": 16}
GLOSSARY = [("Hobbit", "хоббит"), ("Middle-earth", "Средиземье"), ("Dwarves", "гномы")]


def write_pdf(path, pages):
    """pages: список страниц, страница — список (x, y, text, size) или ('image', rect)."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=432, height=648)
        for item in items:
            if item[0] == "image":
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 60), False)
                pix.set_rect(pix.irect, (200, 30, 30))
                page.insert_image(pymupdf.Rect(*item[1]), pixmap=pix)
            else:
                x, y, text, size = item
                page.insert_text((x, y), text, fontsize=size)
    doc.save(path)
    return path


def book_page(number, lines, top=80):
    items = [(200, 40, f"{number} PROLOGUE", 9)]
    y = top
    for x, text in lines:
        items.append((x, y, text, 11))
        y += 14
    return items


def extract_blocks(tmp_path, pages):
    pdf = write_pdf(tmp_path / "t.pdf", pages)
    return extract.extract(pdf, tmp_path / "w", EXTRACT_CFG)


def test_book_layout(tmp_path):
    """Колонтитулы, красная строка, перенос внутри абзаца и абзац через границу страницы."""
    filler = "the quick brown fox jumps over the lazy dog and runs away"
    page1 = [(72, "First words of a long story that keeps going on and on")] + \
            [(60, filler)] * 3 + \
            [(60, "this sentence is beau-"), (60, "tiful and it ends here."),
             (72, "Second paragraph starts now and continues to the page")] + \
            [(60, filler)] * 3 + [(60, "and does not end on this")]
    page2 = [(60, "page but goes on across the boundary.")] + \
            [(72, "Third paragraph is rather short and finishes right here.")]
    blocks = extract_blocks(tmp_path, [book_page(2, page1), book_page(3, page2)])
    texts = [b["text"] for b in blocks]

    assert len(blocks) == 3, texts
    assert all("PROLOGUE" not in t for t in texts)
    assert "beautiful and" in texts[0]
    assert "this page but goes on across the boundary." in texts[1]
    assert texts[2].startswith("Third paragraph")
    assert [b["page"] for b in blocks] == [2 - 1, 1, 2]


def test_section_break_and_compound_hyphen(tmp_path):
    lines = [(72, "A first paragraph about shoe-making runs through this line")]
    lines += [(60, "and the craft of shoe-"), (60, "making is kept with its hyphen."), (72, "Next paragraph begins here and goes on")]
    lines += [(60, "until the sentence ends in the usual way.")]
    page = book_page(2, lines)
    page.append((72, 300, "A later section begins after a visible gap in the text", 11))
    page.append((60, 314, "and it goes on for one more line until the end.", 11))
    blocks = extract_blocks(tmp_path, [page, book_page(3, [(60, "Closing words.")])])
    assert "shoe-making is kept" in blocks[0]["text"]
    section = [b for b in blocks if b["text"].startswith("A later section")]
    assert section and section[0].get("section_break") is True


def test_images_heading_caption(tmp_path):
    page = [
        (60, 90, "The Great Chapter", 20),
        (60, 120, "Body text of the chapter goes on through this first line and more", 11),
        (60, 134, "and the end of the first body paragraph is reached here.", 11),
        ("image", (60, 170, 260, 290)),
        (60, 305, "Figure 1. A red rectangle.", 9),
        (60, 330, "More body text comes after the picture on the same page.", 11),
    ]
    blocks = extract_blocks(tmp_path, [page])
    assert [b["type"] for b in blocks] == ["heading", "paragraph", "image", "caption", "paragraph"]
    asset = tmp_path / "w" / blocks[2]["asset"]
    assert asset.exists() and asset.stat().st_size > 0
    json.dumps(blocks)  # в IR не должно остаться байтов картинки


def test_two_columns_keep_paragraphs(tmp_path):
    page = []
    for col, x in enumerate((50, 320)):
        y = 80
        for para in range(3):
            for line in range(5):
                page.append((x + (12 if line == 0 else 0), y, f"C{col} P{para} line{line} flows on here", 10))
                y += 13
            page.append((x, y, f"Final C{col} P{para}.", 10))
            y += 13
    blocks = extract_blocks(tmp_path, [page])
    assert len(blocks) == 6, [b["text"][:30] for b in blocks]
    for i, block in enumerate(blocks):
        assert block["text"].startswith(f"C{i // 3} P{i % 3} line0")
        assert block["text"].endswith(f"Final C{i // 3} P{i % 3}.")


def test_list_items_are_separate(tmp_path):
    page = [(60, 100 + i * 15, f"- List item number {i} is short.", 10) for i in range(3)]
    assert len(extract_blocks(tmp_path, [page])) == 3


def test_preview_does_not_enqueue(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    p.preview(pdf)
    assert p.store.files() == []
    assert rendered
    p.store.close()


def test_same_model_on_both_passes(project):
    """Режим «всё через 12B»: второго прохода фактически нет, файл всё равно доходит до конца."""
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda m, s: BAD)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    assert {c[0] for c in client.calls} == {"translategemma:12b"}
    assert p.store.get_file(fh)["status"] == "rendered"
    assert len(client.calls) == 2
    assert BAD in rendered[-1]
    p.store.close()


def test_rerender_refuses_without_current_translation(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.store.add_file(pdf)
    assert p.process([fh]) == 0
    count = len(rendered)
    p2 = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    p2.store.set_mode(fh, "quality")
    with pytest.raises(cli.StaleTranslation):
        p2.stage_render(fh)
    assert len(rendered) == count
    p.store.close()
    p2.store.close()


def test_remove_deletes_queue_copy_only(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    p.input_dir.mkdir(exist_ok=True)
    queued = p.input_dir / "q.pdf"
    shutil.copy(pdf, queued)
    fh = p.store.add_file(queued)
    p.process([fh])
    output = p.output_dir / "q.pdf"
    assert output.exists()
    p.remove(fh)
    assert p.store.files() == [] and not queued.exists() and output.exists()
    assert not (p.work_dir / fh).exists()

    outside = p.store.add_file(pdf)
    p.remove(outside)
    assert pdf.exists()
    p.store.close()


def test_fast_mode_needs_only_first_model(project):
    cfg, root, pdf, rendered = project
    checked = []
    client = FakeClient(lambda m, s: BAD)
    client.check = checked.extend
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "fast")
    assert p.process([fh]) == 0
    assert checked == ["translategemma:4b"]
    assert {c[0] for c in client.calls} == {"translategemma:4b"}
    assert p.store.get_file(fh)["rendered_mode"] == "fast"
    p.store.close()


def test_switching_modes_reuses_translations(project):
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda m, s: "12B " + GOOD if "12b" in m else BAD)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "fast")
    assert p.process([fh]) == 0
    assert BAD in rendered[-1] and len(client.calls) == 2
    assert not p.needs_work(p.store.get_file(fh))

    # Сбалансированный режим дополняет: 4B не повторяется, 12B переделывает только помеченное.
    p.store.set_mode(fh, "balanced")
    assert p.needs_work(p.store.get_file(fh))
    assert p.process([fh]) == 0
    assert [c[0] for c in client.calls[2:]] == ["translategemma:12b"] * 2
    assert "12B " + GOOD in rendered[-1]

    # Возврат к быстрому: новых вызовов нет, но PDF пересобран из вариантов 4B.
    calls, renders = len(client.calls), len(rendered)
    p.store.set_mode(fh, "fast")
    assert p.process([fh]) == 0
    assert len(client.calls) == calls and len(rendered) == renders + 1
    assert BAD in rendered[-1] and "12B " not in rendered[-1]
    assert p.store.get_file(fh)["rendered_mode"] == "fast"
    p.store.close()


def test_new_files_get_default_mode_and_keep_it(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.add(pdf)
    assert p.store.get_file(fh)["mode"] == "balanced"
    p.store.set_mode(fh, "quality")
    p.add(pdf)  # повторное добавление не сбрасывает выбор
    assert p.store.get_file(fh)["mode"] == "quality"
    p.store.close()


def test_legacy_rows_are_backfilled(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.store.add_file(pdf)
    p.store.set_status(fh, "rendered")
    p.store.close()
    p2 = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    row = p2.store.get_file(fh)
    assert row["mode"] == "balanced" and row["rendered_mode"] == "balanced"
    p2.store.close()


A4 = {"width": 595, "height": 842}


def footer_doc(path, label, y=766):
    """Как в реальной книге: подпись идёт первой в потоке PDF, а предложение переходит на следующую страницу."""
    doc = pymupdf.open()
    for number, lines in enumerate([
        ["Start of the first paragraph goes on through this line", "and keeps going until it reaches the words"],
        ["that continue on the next page without any break.", "See page 70 for the details of this matter."],
        ["The third page has a line of its own to show here.", "And it finishes with another plain sentence."],
    ], start=70):
        page = doc.new_page(**A4)
        page.insert_text((400, y), label.format(n=number), fontsize=11)
        for i, line in enumerate(lines):
            page.insert_text((72, 80 + i * 16), line, fontsize=11)
    doc.save(path)
    return path


@pytest.mark.parametrize("label", [
    "Page | {n}", "Page {n}", "PAGE {n}", "page: {n}", "Page {n} of 300", "{n} | Page", "{n} | P a g e",
    "P a g e | {n}", "p. {n}", "- Page {n} -",
])
def test_page_labels_are_removed(tmp_path, label):
    blocks = extract.extract(footer_doc(tmp_path / "t.pdf", label), tmp_path / "w", EXTRACT_CFG)
    joined = " ".join(b["text"] for b in blocks)
    for number in (70, 71, 72):
        assert label.format(n=number) not in joined
    assert "the words that continue on the next page" in joined  # подпись не разорвала предложение
    assert "See page 70 for the details" in joined              # обычная фраза в тексте осталась


@pytest.mark.parametrize("text", ["Page | 70", "Page 70 of 300", "70 | P a g e", "p. 70", "Стр. 70", "Страница 70", "стр 70", "— 70 | Page —"])
def test_page_label_pattern_matches(text):
    assert extract.PAGE_LABEL_RE.match(text)


@pytest.mark.parametrize("text", ["Page", "See page 70", "Page 70 was torn out of the book", "70 pages", "Part 2", "Pages of history 3", "Chapter 12", "12"])
def test_page_label_pattern_ignores_text(text):
    assert not extract.PAGE_LABEL_RE.match(text)


def test_other_language_label_by_position(tmp_path):
    """«Seite» шаблон не знает, но подпись стоит на одной высоте на каждой странице."""
    blocks = extract.extract(footer_doc(tmp_path / "t.pdf", "Seite {n}"), tmp_path / "w", EXTRACT_CFG)
    assert "Seite" not in " ".join(b["text"] for b in blocks)


def test_single_label_in_body_is_kept(tmp_path):
    page = [(60, 300, "Page 5", 11), (60, 330, "A normal paragraph of text follows that short line here.", 11)]
    blocks = extract_blocks(tmp_path, [page])
    assert "Page 5" in " ".join(b["text"] for b in blocks)


def test_repeated_body_numbers_are_kept(tmp_path):
    """Числа посреди страницы на разной высоте не колонтитулы."""
    pages = [[(60, 300 + i * 40, f"In the year 14{i} the story goes on as it did before.", 11)] for i in range(4)]
    assert len(" ".join(b["text"] for b in extract_blocks(tmp_path, pages)).split("In the year")) == 5


def _write_blocks(p, fh, blocks, version=None):
    payload = {"page_size": [595, 842], "blocks": blocks}
    if version is not None:
        payload["extract_version"] = version
    (p.work_dir / fh / "blocks.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_old_extraction_is_upgraded_and_reuses_translations(project):
    """Книга переведена со старым разбором: подпись вклеена в текст и сдвинула номера блоков.
    После обновления заново переводится только кусок, текст которого изменился."""
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda m, s: GOOD)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf)
    p.stage_extract(fh)
    fresh = extract.load_blocks(p.work_dir / fh)["blocks"]
    assert [b["text"] for b in fresh][0] == SRC and len(fresh) == 2

    old = [{"id": 0, "type": "paragraph", "page": 1, "text": "Page | 70"}]
    old += [dict(b, id=b["id"] + 1) for b in fresh]
    old[1]["text"] = "Page | 71 " + SRC
    _write_blocks(p, fh, old, extract.EXTRACT_VERSION)
    assert p.process([fh]) == 0
    before = len(client.calls)  # подписи с числами ещё и помечаются, поэтому вызывали и 12B
    assert before > 3

    _write_blocks(p, fh, old)  # разбор предыдущей версии
    assert p.needs_work(p.store.get_file(fh))
    p.mark_outdated()
    assert p.store.get_file(fh)["status"] == "extracted"
    renders = len(rendered)
    assert p.process([fh]) == 0

    assert [c[1] for c in client.calls[before:]] == [SRC]  # второй абзац переехал на другой номер, но не переводится
    assert p.store.get_file(fh)["status"] == "rendered"
    assert extract.load_blocks(p.work_dir / fh)["extract_version"] == extract.EXTRACT_VERSION
    assert not p.needs_work(p.store.get_file(fh))
    assert len(rendered) == renders + 1
    assert "Page |" not in rendered[-1]
    p.store.close()


def test_reconcile_drops_second_pass_of_changed_text(project):
    """Перевод 12B другого абзаца с тем же номером блока не должен попасть в новую книгу."""
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: BAD))
    fh = p.add(pdf)
    p.store.upsert_chunk(fh, 0, 0, "translategemma:12b", cfg["llm"]["prompt_ver"], "old text")
    p.store.reconcile_chunks(fh, [(0, 0, "new text")])
    assert p.store.rows(fh, "translategemma:12b", cfg["llm"]["prompt_ver"]) == []
    p.store.close()


def test_missing_source_keeps_old_extraction(project):
    cfg, root, pdf, rendered = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.add(pdf)
    assert p.process([fh]) == 0
    _write_blocks(p, fh, extract.load_blocks(p.work_dir / fh)["blocks"])
    pdf.unlink()
    row = p.store.get_file(fh)
    assert not p.extract_outdated(row) and not p.needs_work(row)
    p.store.close()


@pytest.mark.parametrize("mode,model", [("fast", "translategemma:4b"), ("balanced", "translategemma:12b"), ("quality", "translategemma:12b")])
def test_changed_glossary_is_applied_in_every_mode(project, mode, model):
    """Смена перевода термина должна доходить до PDF, даже если последняя модель уже видела этот кусок."""
    cfg, root, pdf, rendered = project

    def translate(m, src):
        # Модель следует глоссарию из промпта.
        return f"Тихо жил {'хоббит' if client.calls[-1][2].find('Hobbit = хоббит') >= 0 else 'невысоклик'} в Средиземье долгие годы, прежде чем о нём узнали другие."

    client = FakeClient(translate)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, mode)
    assert p.process([fh]) == 0
    assert "хоббит" in rendered[-1] and "невысоклик" not in rendered[-1]
    p.store.close()

    (root / "glossary.tsv").write_text("Hobbit\tневысоклик\nMiddle-earth\tСредиземье\n", encoding="utf-8")
    p = cli.Pipeline(cfg, root, client)
    calls = len(client.calls)
    assert p.process([fh]) == 0
    assert model in [c[0] for c in client.calls[calls:]]
    assert "невысоклик" in rendered[-1] and "хоббит" not in rendered[-1]
    assert p.store.get_file(fh)["status"] == "rendered"

    # Следующий запуск ничего не повторяет.
    calls = len(client.calls)
    assert p.process([fh]) == 0
    assert len(client.calls) == calls
    p.store.close()


def test_stubborn_model_is_not_retried_forever(project):
    """Модель игнорирует термин: с тем же глоссарием кусок не переводится при каждом запуске."""
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda m, s: BAD)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    calls = len(client.calls)
    assert p.process([fh]) == 0
    assert len(client.calls) == calls
    p.store.close()


@pytest.mark.parametrize("source,target,expected", [
    ("Gabil", "Габил", [("Gabil", "Габил")]),
    ("Ciel, Raphael", "Сиэль, Рафаэль", [("Ciel", "Сиэль"), ("Raphael", "Рафаэль")]),
    ("Gazel (King Gazel)", "Газель (король Газель)", [("Gazel", "Газель"), ("King Gazel", "король Газель")]),
    ("Oboro (Crestwater)", "Оборо", [("Oboro", "Оборо"), ("Crestwater", "Оборо")]),
    ("dismiss (words)", "отмахнуться, отвергнуть", [("dismiss", "отмахнуться, отвергнуть")]),
    ("go (be) on the move", "вступить в бой", [("go on the move", "вступить в бой")]),
    ("awakened / awakening", "пробуждённый / пробуждение", [("awakened", "пробуждённый"), ("awakening", "пробуждение")]),
    ("Special A / A / over-A", "особый A / A / выше A", [("Special A", "особый A"), ("over-A", "выше A")]),
    ("Faust, Lord of Investigation", "Фауст, владыка исследования", [("Faust", "Фауст"), ("Lord of Investigation", "владыка исследования")]),
    ("Single Digits", "«однозначные»", [("Single Digits", "однозначные")]),
    ("No wonder…", "Неудивительно, что…", [("No wonder", "Неудивительно, что…")]),
    ("parry", "парировать, отбить удар", [("parry", "парировать, отбить удар")]),
    ("Heh, Hee, Keh", "смешок", []),
])
def test_glossary_expand(source, target, expected):
    assert glossary_import.expand(source, target) == expected


def test_glossary_expand_ones():
    pairs = glossary_import.expand("let one's guard down", "расслабиться")
    assert ("let his guard down", "расслабиться") in pairs and ("let their guard down", "расслабиться") in pairs


@pytest.mark.parametrize("source,target,strict", [
    ("Marco", "Марко", True),
    ("Demon Lord", "король демонов", True),
    ("Primal", "первородный демон", True),
    ("Empire", "Империя", True),
    ("Unique", "уникальный", False),      # обычное слово с заглавной
    ("Absorb", "поглощение", False),
    ("magicule count", "количество магикул", False),
    ("harass", "донимать, изматывать (не «преследовать»)", False),
    ("No wonder", "Неудивительно, что…", False),
])
def test_glossary_is_strict(source, target, strict):
    assert glossary_import.is_strict(source, target) is strict


def make_workbook(path, rows):
    from openpyxl import Workbook
    book = Workbook()
    for row in rows:
        book.active.append(row)
    book.save(path)
    return path


GLOSSARY_ROWS = [
    ("Категория", "English", "Русский (рекомендуемый)", "Пояснение"),
    ("Персонажи", "Marco", "Марко", "Боец"),
    ("Персонажи", "Hobbit", "Невысоклик", "Заменяет прежний перевод"),
    ("Лексика", "abandon", "бросить", None),
    ("Звуки и междометия", "Tsk", "цоканье языком", None),
    (None, None, None, None),
    ("Источник: перевод тома", None, None, None),
]


def test_import_file_is_idempotent(tmp_path):
    book = make_workbook(tmp_path / "g.xlsx", GLOSSARY_ROWS)
    gloss = tmp_path / "glossary.tsv"
    hints = tmp_path / "hints.tsv"
    gloss.write_text("# заметка\nHobbit\tхоббит\nShire\tШир\n", encoding="utf-8")
    result = glossary_import.import_file(book, gloss, hints)
    assert (result["added"], result["updated"], result["hintsAdded"]) == (1, 1, 1)
    assert result["skipped"] == ["Tsk"]
    assert gloss.read_text(encoding="utf-8") == "# заметка\nHobbit\tНевысоклик\nShire\tШир\nMarco\tМарко\n"
    assert hints.read_text(encoding="utf-8") == "abandon\tбросить\n"
    again = glossary_import.import_file(book, gloss, hints)
    assert (again["added"], again["updated"], again["hintsAdded"], again["hintsUpdated"]) == (0, 0, 0, 0)


def test_import_without_header(tmp_path):
    book = make_workbook(tmp_path / "g.xlsx", [("Marco", "Марко"), ("Gabil", "Габил")])
    assert glossary_import.read_workbook(book).terms == [("Marco", "Марко"), ("Gabil", "Габил")]


def test_verb_forms_match_only_for_vocabulary():
    assert llm.glossary_for("he harassed them", [("harass", "донимать")])
    assert llm.glossary_for("while abandoning it", [("abandon", "бросить")])
    assert not llm.glossary_for("Tooked", [("Took", "Тук")])
    assert not llm.glossary_for("the embarrassed man", [("harass", "донимать")])


def test_hints_reach_prompt_but_are_not_required(project):
    cfg, root, pdf, rendered = project
    (root / "hints.tsv").write_text("quietly\tтихонько\n", encoding="utf-8")
    client = FakeClient(lambda m, s: GOOD)  # в переводе нет слова «тихонько»
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf)
    assert p.process([fh]) == 0
    assert any("quietly = тихонько" in c[2] for c in client.calls)
    assert all("quietly = тихонько" not in c[2] for c in client.calls if "second" in c[1].lower())
    flagged = [r for r in p.store.rows(fh, "translategemma:4b", cfg["llm"]["prompt_ver"]) if r["flagged"]]
    assert all("quietly" not in (r["reasons"] or "") for r in flagged)
    p.store.close()


NAME_TEXTS = [
    "This wasn't like Copy, part of Hinata’s unique skill Usurper, and the Intelligence Bureau knew.",
    "The Emperor Rudra sent Marco. Later the emperor left with Sir Gobta to the River and the great River.",
    "Hinata spoke. Then Hobbits and Tooks came, and the Masters of the Wise went North-West.",
    "A copy of Nothing was here. The word Nothing came again, then nothing at all.",
    "It Was A Chapter Heading With Capitals Dracolord",
    "He met Marco twice, and Marco smiled.",
]


def test_find_names():
    found = names.find_names(NAME_TEXTS, [("Hobbit", "хоббит"), ("Took", "Тук"), ("Marco", "Марко")], ["Wise"])
    assert {"Hinata", "Usurper", "Intelligence Bureau", "Rudra", "Gobta"} <= set(found)
    for word in ("Copy", "Emperor", "Sir", "River", "Hobbits", "Tooks", "Marco", "Masters", "Wise", "North-West",
                 "Nothing", "A", "Dracolord", "The", "Hinata’s"):
        assert word not in found


def test_sounds_titles_and_multiword_names():
    texts = [
        "He said Kwah-ha-ha-ha and then Heh, but B-but came, and Uh-huh too, and Mmm and Ee-hee-hee.",
        "They told Your Majesty about the Yen, and she used Probability Manipulation with Vega.",
        "The probability was high and your manipulation was easy.",
    ]
    assert names.find_names(texts) == ["Probability Manipulation", "Vega"]


def test_name_candidates_have_examples():
    items = {c["term"]: c for c in names.candidates(NAME_TEXTS)}
    assert items["Hinata"]["count"] == 2
    assert "Usurper" in items["Usurper"]["example"]


def test_mask_and_restore():
    masked, tokens = names.mask("See [1]. Hinata’s skill Adapter, and Adapters of Hinata. Adapt.", ["Hinata", "Adapter"])
    assert masked == "See [1]. ZXQ1’s skill ZXQ2, and ZXQ3 of ZXQ1. Adapt."
    assert names.restore("См. [1]. Навык ZXQ2 и zxq3 у ZXQ1.", tokens) == "См. [1]. Навык Adapter и Adapters у Hinata."


def test_mask_prefers_longest_name():
    masked, tokens = names.mask("Vortex Crash and Vortex", ["Vortex", "Vortex Crash"])
    assert masked == "ZXQ1 and ZXQ2" and tokens == {"1": "Vortex Crash", "2": "Vortex"}


NAME_SRC = "Later that evening the old servant Hinata used her skill Usurper against the guards at the gate."


def name_project(project, translate):
    cfg, root, _, rendered = project
    cfg["llm"]["keep_names"] = True
    pdf = write_pdf(root / "names.pdf", [[(20, 100, NAME_SRC, 9)]])
    client = FakeClient(translate)
    return cli.Pipeline(cfg, root, client), client, pdf, rendered


def marks_translation(model, src):
    return "Позже тем вечером старая служанка ZXQ1 применила свой навык ZXQ2 против стражников у ворот."


def test_kept_names_never_reach_model(project):
    p, client, pdf, rendered = name_project(project, marks_translation)
    fh = p.add(pdf)
    assert p.process([fh]) == 0
    prompt = client.calls[0][2]
    assert "Hinata" not in prompt and "Usurper" not in prompt and "ZXQ1" in prompt
    assert {c[0] for c in client.calls} == {"translategemma:4b"}  # латиница имён не считается замечанием
    assert "служанка Hinata применила свой навык Usurper" in rendered[-1]
    p.store.close()


def test_lost_mark_goes_to_second_model(project):
    def translate(model, src):
        if "4b" in model:
            return "Позже тем вечером старая служанка ZXQ1 применила свой особый навык против стражников у ворот."
        return marks_translation(model, src)
    p, client, pdf, rendered = name_project(project, translate)
    fh = p.add(pdf)
    assert p.process([fh]) == 0
    assert [c[0] for c in client.calls] == ["translategemma:4b", "translategemma:12b"]
    assert "навык Usurper" in rendered[-1]
    p.store.close()


def test_invented_mark_is_flagged():
    assert any("лишние метки" in r for r in validate.check("Text here", "Текст ZXQ7 здесь", False, [], VALID_CFG))


def test_same_translation_term_is_masked(project):
    cfg, root, _, _ = project
    with (root / "glossary.tsv").open("a", encoding="utf-8") as f:
        f.write("Usurper\tUsurper\n")
    p, client, pdf, rendered = name_project(project, marks_translation)
    fh = p.add(pdf)
    assert p.process([fh]) == 0
    assert "Usurper = Usurper" not in client.calls[0][2] and "Usurper" not in client.calls[0][2]
    assert "навык Usurper" in rendered[-1] or "служанка Usurper" in rendered[-1]
    p.store.close()


def test_plain_name_is_translated_again(project):
    """Слово, которое попросили переводить, переводится заново даже без других замечаний."""
    p, client, pdf, rendered = name_project(project, marks_translation)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    p.store.close()
    cfg, root, _, _ = project
    (root / "plain_names.txt").write_text("Usurper\n", encoding="utf-8")
    translated = "Позже тем вечером старая служанка ZXQ1 применила свой навык Узурпатор против стражников у ворот."
    client = FakeClient(lambda m, s: translated)
    p = cli.Pipeline(cfg, root, client)
    p.store.set_status(fh, "extracted")
    assert p.process([fh]) == 0
    assert "Узурпатор" in rendered[-1]
    p.store.close()


def test_encrypted_pdf_is_reported(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secret")
    path = tmp_path / "e.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(extract.ExtractError):
        extract.extract(path, tmp_path / "w", EXTRACT_CFG)


@pytest.mark.parametrize("text,expected", [
    ('He said "Stop." Then he left. Mr. Baggins stayed.', ['He said "Stop."', "Then he left.", "Mr. Baggins stayed."]),
    ("Short one.", ["Short one."]),
])
def test_split_sentences(text, expected):
    assert chunk.split_sentences(text) == expected


def test_split_text_keeps_all_words_and_limit():
    text = " ".join(f"Sentence number {i} has several plain words in it." for i in range(200))
    parts = chunk.split_text(text, 100)
    assert len(parts) > 1
    assert all(chunk.estimate_tokens(p) <= 100 for p in parts)
    assert " ".join(parts) == text


def test_split_text_without_punctuation():
    text = "word " * 2000
    parts = chunk.split_text(text.strip(), 100)
    assert all(chunk.estimate_tokens(p) <= 100 for p in parts)
    assert " ".join(parts).split() == text.split()


def test_numeric_block_is_not_translated():
    blocks = [{"id": 0, "type": "paragraph", "text": "1234"}, {"id": 1, "type": "image", "asset": "a"},
              {"id": 2, "type": "paragraph", "text": "Words."}]
    assert chunk.build_chunks(blocks, 400) == [(2, 0, "Words.")]


VALID_CFG = {"min_length_ratio": 0.7, "max_length_ratio": 2.0, "short_block_chars": 40,
             "max_latin_share": 0.08, "ngram_size": 4, "ngram_max_repeats": 3}
SRC = "The Hobbit lived quietly in Middle-earth for many long years before others knew."


@pytest.mark.parametrize("dst,marker", [
    ("Хоббит тихо жил в Средиземье долгие годы, прежде чем о нём узнали другие народы мира.", None),
    ("Хоббиты тихо жили в Средиземье много долгих лет, прежде чем о них узнали другие.", None),
    ("", "пустой"),
    ("Хоббит в Средиземье.", "отношение длин"),
    ("Хоббит lived quietly in Средиземье for many long years before others knew about.", "латиница"),
    ("Маленький народец тихо жил в этой земле долгие годы, прежде чем о нём узнали другие.", "нет терминов"),
    ("хоббит в Средиземье жил там " * 5, "повтор"),
])
def test_validate(dst, marker):
    reasons = validate.check(SRC, dst, False, GLOSSARY, VALID_CFG)
    if marker is None:
        assert reasons == []
    else:
        assert any(marker in r for r in reasons), reasons


@pytest.mark.parametrize("translation,term,expected", [
    ("Согласно Алой Книге он был там.", "Алая Книга", True),
    ("Они пришли из Шира и ушли.", "Шир", True),
    ("Жили гномы и эльфы.", "гномы", True),
    ("Он широко улыбнулся и ушёл.", "Тук", False),
    ("Там жил Ток, сын вождя.", "Тук", False),
    ("Проклятие «Рождение мёртвых» всё ещё действовало.", "Рождение мёртвых", True),
    ("Проклятие «Рождения мёртвых» спало.", "Рождение мёртвых", True),
    ("Проклятие «Мертвый День рождения» спало.", "Рождение мёртвых", False),
    ("Вскоре явились два демона-пэра.", "демон-пэр", True),
    ("Он сражался с рыцарями-химерами.", "рыцари-химеры", True),
    ("Она встретилась с Демоном-колоссом.", "колосс", True),
    ("«Д-Диабло… Вы пришли?»", "Диабло", True),
    ("Сила достигла уровня Святого.", "уровень Святого", True),
    ("Сила достигла уровня мудреца.", "уровень Святого", False),
])
def test_term_present(translation, term, expected):
    assert validate.term_present(translation, term) is expected


def test_term_inside_longer_term_is_not_required():
    glossary = [("Chevalier", "шевалье"), ("Diable Chevaliers", "диабль-шевалье")]
    assert validate.required_terms("They became Diable Chevaliers.", glossary) == [("Diable Chevaliers", "диабль-шевалье")]
    assert validate.required_terms("A Chevalier and the Diable Chevaliers.", glossary) == glossary


def test_units_and_numbers():
    src = "He was 4 feet tall in the year 1,420 of the Shire, and it was a long time before others."
    converted = "Он был ростом полтора метра в 1420 году Шира, и прошло много времени до того, как пришли другие."
    assert any("пересчитаны" in r for r in validate.check(src, converted, False, [], VALID_CFG))
    assert validate.missing_numbers(src, "Он был ростом 4 фута в 1420 году.") == []
    assert validate.missing_numbers(src, "Он был высоким в 1420 году.") == ["4"]
    assert not validate.units_converted("It was 3 metres or 10 feet.", "Это 3 метра.")


@pytest.mark.parametrize("src,dst,expected", [
    ("Plain text.", "«Простой текст.»", "Простой текст."),
    ("Plain text.", '"Простой текст."', "Простой текст."),
    ("Plain text.", "«Один» и «два»", "«Один» и «два»"),
    ('"Quoted," he said.', "«Цитата», — сказал он.", "«Цитата», — сказал он."),
    ('"Quoted."', "«Цитата.»", "«Цитата.»"),
    ("Plain.", "  Просто.\n", "Просто."),
])
def test_clean_output(src, dst, expected):
    assert llm.clean_output(src, dst) == expected


def test_validate_truncated():
    dst = "Хоббит тихо жил в Средиземье долгие годы, прежде чем о нём узнали другие народы мира."
    assert any("обрезан" in r for r in validate.check(SRC, dst, True, GLOSSARY, VALID_CFG))


def test_glossary_matching_is_selective():
    assert llm.glossary_for("Hobbits and Men", [("Hobbit", "х"), ("Men", "л")]) == [("Hobbit", "х"), ("Men", "л")]
    assert llm.glossary_for("many men and mentions", [("Men", "л")]) == []


def test_prompt_format():
    prompt = llm.build_prompt("Hello.", {"name": "English", "code": "en"}, {"name": "Russian", "code": "ru"},
                              [("Hobbit", "хоббит")], "Прошлый абзац.")
    assert prompt.endswith("into Russian:\n\n\nHello.")
    assert "Hobbit = хоббит" in prompt and "Прошлый абзац." in prompt


class FakeClient:
    """Перевод по таблице. Считает вызовы, чтобы проверить идемпотентность."""
    def __init__(self, translate):
        self.fn = translate
        self.calls = []
        self.loaded = None

    def check(self, models):
        pass

    def unload(self, model):
        pass

    def translate(self, model, prompt, src):
        self.calls.append((model, src, prompt))
        self.loaded = model
        return llm.Result(self.fn(model, src), False, 0.01)


GOOD = "Хоббит тихо жил в Средиземье долгие годы, прежде чем о нём узнали другие народы мира."
BAD = "Маленький народец тихо жил в этой земле долгие годы, прежде чем о нём узнали другие."


@pytest.fixture
def project(tmp_path, monkeypatch):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["llm"]["keep_names"] = False  # проверяется отдельно: иначе в простых текстах появятся лишние требования
    (tmp_path / "glossary.tsv").write_text("Hobbit\tхоббит\nMiddle-earth\tСредиземье\n", encoding="utf-8")
    rendered = []
    monkeypatch.setattr(render, "html_to_pdf", lambda html, out, *a: (Path(out).parent.mkdir(exist_ok=True), Path(out).write_bytes(b"%PDF"), rendered.append(Path(html).read_text(encoding="utf-8"))))
    pdf = write_pdf(tmp_path / "doc.pdf", [[(20, 100, SRC, 9), (20, 200, "Second <b>block</b> about a Hobbit and Middle-earth here.", 9)]])
    return cfg, tmp_path, pdf, rendered


def test_two_passes_and_idempotency(project):
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda model, src: GOOD if "4b" not in model or src == SRC else BAD)
    p = cli.Pipeline(cfg, root, client)
    fh = p.store.add_file(pdf)
    assert p.process([fh]) == 0
    assert p.store.get_file(fh)["status"] == "rendered"
    models = [c[0] for c in client.calls]
    assert models.count("translategemma:4b") == 2
    assert models.count("translategemma:12b") == 1
    assert BAD not in rendered[-1] and GOOD in rendered[-1]

    # Второй запуск ничего не переводит и не пересобирает.
    n, r = len(client.calls), len(rendered)
    assert p.process([fh]) == 0
    assert len(client.calls) == n and len(rendered) == r

    # Смена версии промпта переводит заново, но только новым ключом.
    cfg["llm"]["prompt_ver"] += 1
    p2 = cli.Pipeline(cfg, root, client)
    assert p2.process([fh]) == 0
    assert len(client.calls) == n + 3
    p.store.close()
    p2.store.close()


def test_pass2_worse_keeps_pass1(project):
    cfg, root, pdf, rendered = project
    worst = "x"
    client = FakeClient(lambda model, src: BAD if "4b" in model else worst)
    p = cli.Pipeline(cfg, root, client)
    fh = p.store.add_file(pdf)
    assert p.process([fh]) == 0
    assert BAD in rendered[-1]
    p.store.close()


def test_error_resumes(project):
    cfg, root, pdf, rendered = project
    cfg["llm"]["retries"] = 0
    fail = {"on": True}

    def translate(model, src):
        if fail["on"] and src != SRC:
            raise ValueError("boom")
        return GOOD

    client = FakeClient(translate)
    p = cli.Pipeline(cfg, root, client)
    fh = p.store.add_file(pdf)
    assert p.process([fh]) == 1
    assert p.store.get_file(fh)["status"] == "extracted"
    assert rendered == []
    done_before = len(client.calls)

    fail["on"] = False
    assert p.process([fh]) == 0
    assert p.store.get_file(fh)["status"] == "rendered"
    assert len(client.calls) == done_before + 1  # готовый чанк не переводится повторно
    p.store.close()


def test_html_escapes_text(project):
    cfg, root, pdf, rendered = project
    client = FakeClient(lambda model, src: "<script>alert(1)</script> " + GOOD)
    p = cli.Pipeline(cfg, root, client)
    p.process([p.store.add_file(pdf)])
    assert "<script>" not in rendered[-1] and "&lt;script&gt;" in rendered[-1]
    p.store.close()


def test_real_fragment():
    pdf = ROOT / "test-fragment" / "fragment.pdf"
    if not pdf.exists():
        pytest.skip("нет фрагмента")
    work = ROOT / ".pytest_work"
    try:
        blocks = extract.extract(pdf, work, EXTRACT_CFG)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    joined = " ".join(b["text"] for b in blocks)
    assert "prologue" not in joined.lower()
    assert "beautiful, broad" in joined
    assert "after their own fashion" in joined          # абзац через границу страницы
    assert "and of a shadow that fell" in joined
    assert "shoe-making" in joined
    assert "historical" not in joined and "history, in which" in joined
    assert sum(1 for b in blocks if b.get("section_break")) == 1
    assert len(blocks) == 9


# --- персонажи и пол ---------------------------------------------------------
GENDER_BOOK = [
    "Lady Shuna entered the hall. Shuna smiled as she sat down. Shuna nodded and she poured the tea.",
    "Shuna looked up. She said nothing, and Vega laughed as he drew his sword. Vega walked away. He was angry.",
    "“She is wrong,” said Vega. Vega nodded as he turned. “Her? Never,” said Ciel. Ciel nodded. Ciel smiled.",
    "They reached the Tempest Gate. The Tempest Gate opened in Tempest. In Tempest the Tempest flag rose.",
]


def test_gender_is_inferred_from_narration_only():
    found = characters.analyze(GENDER_BOOK, ["Shuna", "Vega", "Ciel", "Tempest"])
    assert (found["Shuna"]["person"], found["Shuna"]["gender"]) == (True, "f")
    assert (found["Vega"]["person"], found["Vega"]["gender"]) == (True, "m")
    # Местоимения в реплике («Her? Never») не считаются: пол остаётся пустым, его заполнит человек.
    assert (found["Ciel"]["person"], found["Ciel"]["gender"]) == (True, "")
    assert found["Tempest"]["person"] is False


def test_person_choice_beats_inference():
    genders = characters.effective(GENDER_BOOK, ["Shuna", "Vega", "Ciel"], {"Vega": "f", "Ciel": "m", "Shuna": "x"})
    assert genders == {"Vega": "f", "Ciel": "m"}
    rows = {r["term"]: r for r in characters.report(GENDER_BOOK, ["Shuna", "Ciel", "Tempest"], {"Tempest": "f", "Shuna": "x"})}
    assert rows["Tempest"]["person"] is True and rows["Shuna"]["person"] is False


def test_titles_and_long_forms_share_gender():
    assert characters.base_name("Sir Rimuru") == "Rimuru"
    assert characters.gender_of("Sir Rimuru", {"Rimuru": "m"}) == "m"
    assert characters.gender_of("Rimuru Tempest", {"Rimuru": "m"}) == "m"
    assert characters.gender_of("Vega", {"Rimuru": "m"}) == ""
    rows = characters.report(["Rimuru Tempest arrived. Rimuru nodded."], ["Rimuru", "Rimuru Tempest", "Demon Lord"], {})
    assert [r["term"] for r in rows] == ["Rimuru"] and rows[0]["aliases"] == ["Rimuru Tempest"]


def test_annotate_marks_and_glossary():
    lines = characters.annotate([("Shuna", "Шуна"), ("Hobbit", "хоббит"), ("sword", "меч")], {"1": "Vega", "2": "Storm"}, {"Shuna": "f", "Vega": "m"})
    assert lines == [("ZXQ1", "ZXQ1 (male, он)"), ("Shuna", "Шуна (female, она)"), ("Hobbit", "хоббит"), ("sword", "меч")]


@pytest.mark.parametrize("dst,gender,wrong", [
    ("Шион кивнул и вышел.", "f", True),
    ("Шион медленно кивнул.", "f", True),
    ("«Стой», — сказал Шион.", "f", True),
    ("Шион кивнула и вышла.", "f", False),
    ("Он увидел Шион и кивнул.", "f", False),
    ("Шион генерал армии.", "f", False),
    ("Шион кивнула.", "m", True),
    ("Шион кивнул.", "m", False),
    ("Шион сила и надежда.", "m", False),
])
def test_gender_conflicts(dst, gender, wrong):
    assert characters.gender_conflicts(dst, [("Shion", "Шион", gender)]) == (["Shion"] if wrong else [])


def test_gender_reaches_prompt_and_wrong_gender_is_retranslated(project):
    cfg, root, _, rendered = project
    cfg["llm"]["keep_names"] = True
    (root / "glossary.tsv").write_text("Shuna\tШуна\n", encoding="utf-8")
    (root / "characters.tsv").write_text("Shuna\tf\nVega\tm\n", encoding="utf-8")
    src = "Later that evening Shuna nodded to the guards, and then Vega opened the old gate for the whole party."
    pdf = write_pdf(root / "g.pdf", [[(20, 100, src, 9)]])
    answers = ["Позже тем вечером Шуна кивнул стражникам, а затем ZXQ1 открыл старые ворота для всего отряда."]
    answers.append("Позже тем вечером Шуна кивнула стражникам, а затем ZXQ1 открыл старые ворота для всего отряда.")
    client = FakeClient(lambda model, s: answers[0] if "4b" in model else answers[1])
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "balanced")
    assert p.process([fh]) == 0
    assert "Shuna = Шуна (female, она)" in client.calls[0][2] and "ZXQ1 = ZXQ1 (male, он)" in client.calls[0][2]
    assert [c[0] for c in client.calls] == ["translategemma:4b", "translategemma:12b"]
    assert "Шуна кивнула" in rendered[-1] and "Vega открыл" in rendered[-1]
    p.store.close()


def test_new_gender_does_not_touch_clean_chunks(project):
    cfg, root, _, _ = project
    (root / "glossary.tsv").write_text("Shuna\tШуна\n", encoding="utf-8")
    src = "Later that evening Shuna walked to the guards, and then opened the old gate for the whole party."
    pdf = write_pdf(root / "c.pdf", [[(20, 100, src, 9)]])
    good = "Позже тем вечером Шуна подошла к стражникам, а затем открыла старые ворота для всего отряда."
    client = FakeClient(lambda m, s: good)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    n = len(client.calls)
    p.store.close()
    (root / "characters.tsv").write_text("Shuna\tf\n", encoding="utf-8")
    p = cli.Pipeline(cfg, root, client)
    assert p.process([fh]) == 0
    assert len(client.calls) == n
    p.store.close()


def test_no_gender_keeps_prompt_and_hash(project):
    cfg, root, pdf, _ = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.add(pdf)
    p.stage_extract(fh)
    assert p._terms_hash(fh, "A Hobbit here") == cli.sha("Hobbit=хоббит")
    p.store.close()


def test_new_gender_fixes_finished_book_once(project):
    """«Применить глоссарий»: переводится заново только кусок с ошибкой рода, упрямый кусок не зацикливается."""
    cfg, root, _, rendered = project
    (root / "glossary.tsv").write_text("Shuna\tШуна\n", encoding="utf-8")
    wrong_src = "Later that evening Shuna nodded to the guards, and then opened the old gate for the whole party."
    clean_src = "Later that evening Shuna walked to the guards, and then closed the old gate for the whole party."
    pdf = write_pdf(root / "w.pdf", [[(20, 100, wrong_src, 9), (20, 300, clean_src, 9)]])
    wrong = "Позже тем вечером Шуна кивнул стражникам, а затем открыл старые ворота для всего отряда."
    clean = "Позже тем вечером Шуна подошла к стражникам, а затем закрыла старые ворота для всего отряда."
    client = FakeClient(lambda m, s: wrong if "nodded" in s else clean)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    assert len(client.calls) == 2
    p.store.close()

    (root / "characters.tsv").write_text("Shuna\tf\n", encoding="utf-8")
    fixed = "Позже тем вечером Шуна кивнула стражникам, а затем открыла старые ворота для всего отряда."
    client = FakeClient(lambda m, s: fixed)
    p = cli.Pipeline(cfg, root, client)
    assert p.process([fh]) == 0
    assert len(client.calls) == 1 and "nodded" in client.calls[0][2] and "(female, она)" in client.calls[0][2]
    assert "Шуна кивнула" in rendered[-1] and "Шуна подошла" in rendered[-1]
    p.store.close()

    # Модель упорно ошибается: одного повтора достаточно, новый запуск кусок не трогает.
    (root / "characters.tsv").write_text("Shuna\tm\n", encoding="utf-8")
    client = FakeClient(lambda m, s: fixed if "nodded" in s else clean)
    p = cli.Pipeline(cfg, root, client)
    assert p.process([fh]) == 0
    n = len(client.calls)
    assert n == 2  # оба куска теперь расходятся с мужским родом
    p.store.close()
    p = cli.Pipeline(cfg, root, client)
    assert p.process([fh]) == 0
    assert len(client.calls) == n
    p.store.close()


def test_retranslate_gendered_redoes_only_stale_chunks(project):
    cfg, root, _, rendered = project
    (root / "glossary.tsv").write_text("Shuna\tШуна\n", encoding="utf-8")
    with_name = "Later that evening Shuna walked to the guards, and then closed the old gate for the whole party."
    no_name = "Later that evening the captain walked to the guards, and then closed the old gate for the whole party."
    pdf = write_pdf(root / "r.pdf", [[(20, 100, with_name, 9), (20, 300, no_name, 9)]])
    first = "Позже тем вечером Шуна подошёл к стражникам, а потом закрыл старые ворота для всего отряда у стены."
    other = "Позже тем вечером капитан подошёл к стражникам, а потом закрыл старые ворота для всего отряда."
    client = FakeClient(lambda m, s: first if "Shuna" in s else other)
    p = cli.Pipeline(cfg, root, client)
    fh = p.add(pdf, "quality")
    assert p.process([fh]) == 0
    p.store.close()

    (root / "characters.tsv").write_text("Shuna\tf\n", encoding="utf-8")
    second = "Позже тем вечером к стражникам подошла Шуна, а потом закрыла старые ворота для всего отряда."
    client = FakeClient(lambda m, s: second)
    p = cli.Pipeline(cfg, root, client)
    assert p.retranslate_gendered(fh) == 1
    # Старый текст не пропадает, пока нет нового.
    row = next(r for r in p.store.rows(fh, "translategemma:12b", p.ver) if r["status"] == "pending")
    assert row["dst"] == first
    assert p.process([fh]) == 0
    assert len(client.calls) == 1 and "(female, она)" in client.calls[0][2]
    assert "подошла Шуна" in rendered[-1] and "капитан подошёл" in rendered[-1]
    assert p.retranslate_gendered(fh) == 0
    p.store.close()


# --- группы книг ---------------------------------------------------------------
def group_project(project):
    cfg, root, _, _ = project
    (root / "glossary.tsv").write_text("Hobbit\tхоббит\n", encoding="utf-8")
    (root / "groups.json").write_text(json.dumps({"groups": [{"id": "slime", "name": "Slime"}], "books": {}}), encoding="utf-8")
    folder = root / "context" / "slime"
    folder.mkdir(parents=True)
    (folder / "glossary.tsv").write_text("Hobbit\tхоббитец\nShuna\tШуна\n", encoding="utf-8")
    (folder / "hints.tsv").write_text("Tempest\tТемпест\n", encoding="utf-8")
    (folder / "characters.tsv").write_text("Shuna\tf\n", encoding="utf-8")
    return cfg, root


def test_each_group_has_own_context(project):
    cfg, root = group_project(project)
    text = "Later that evening a Hobbit and Shuna walked to Tempest, and then closed the old gate for the party."
    first = write_pdf(root / "one.pdf", [[(20, 100, text, 9)]])
    second = write_pdf(root / "two.pdf", [[(20, 100, text + " It rained.", 9)]])
    client = FakeClient(lambda m, s: "Позже тем вечером хоббит с Шуной пошли в Темпест, а затем закрыли старые ворота для отряда.")
    p = cli.Pipeline(cfg, root, client)
    plain_book, slime_book = p.add(first, "fast"), p.add(second, "fast")
    p.store.close()
    (root / "groups.json").write_text(json.dumps({"groups": [{"id": "slime", "name": "Slime"}], "books": {slime_book: "slime"}}), encoding="utf-8")

    p = cli.Pipeline(cfg, root, client)
    assert p.process([plain_book]) == 0
    assert "Hobbit = хоббит" in client.calls[-1][2]
    assert "хоббитец" not in client.calls[-1][2] and "Шуна" not in client.calls[-1][2] and "Темпест" not in client.calls[-1][2]
    assert p.process([slime_book]) == 0
    assert "Hobbit = хоббитец" in client.calls[-1][2]
    assert "Shuna = Шуна (female, она)" in client.calls[-1][2] and "Tempest = Темпест" in client.calls[-1][2]
    assert p.genders(plain_book).get("Shuna") != "f"
    assert p.names_report(slime_book)["group"] == "slime"
    assert p.names_report(plain_book)["group"] == "default"
    p.store.close()


def test_book_without_groups_file_uses_root_context(project):
    cfg, root, pdf, _ = project
    p = cli.Pipeline(cfg, root, FakeClient(lambda m, s: GOOD))
    fh = p.add(pdf)
    assert p.context(fh).group == "default" and ("Hobbit", "хоббит") in p.context(fh).glossary
    p.store.close()


def test_book_in_missing_group_is_an_error(tmp_path):
    groups = {"groups": [], "books": {"abc": "gone"}}
    with pytest.raises(SystemExit):
        context.group_of(groups, "abc")
    assert context.group_of(groups, "other") == "default"


def test_damaged_groups_file_is_an_error(tmp_path):
    (tmp_path / "groups.json").write_text("{сломано", encoding="utf-8")
    with pytest.raises(SystemExit):
        context.load_groups(tmp_path)


def test_group_files_keep_default_paths(tmp_path):
    paths = {"glossary": "glossary.tsv", "hints": "hints.tsv", "plain_names": "plain_names.txt"}
    assert context.files_for(tmp_path, paths, "default")["glossary"] == tmp_path / "glossary.tsv"
    files = context.files_for(tmp_path, paths, "slime")
    assert files["glossary"] == tmp_path / "context" / "slime" / "glossary.tsv"
    assert files["characters"] == tmp_path / "context" / "slime" / "characters.tsv"


def test_full_page_picture_does_not_split_paragraph(tmp_path):
    """Иллюстрация на всю страницу посреди абзаца встаёт после абзаца. Картинка за краем страницы не теряется."""
    doc = pymupdf.open()
    first = doc.new_page()
    first.insert_text((50, 100), "The first page has a short opening paragraph that ends here.", fontsize=11)
    first.insert_text((50, 130), "This paragraph runs to the very bottom of the page and then", fontsize=11)
    picture = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 60), 0)
    pix.clear_with(200)
    picture.insert_image(pymupdf.Rect(0, -3, picture.rect.width, picture.rect.height + 2), pixmap=pix)
    last = doc.new_page()
    last.insert_text((50, 60), "continues on the next page without any break at all.", fontsize=11)
    last.insert_text((50, 90), "A new paragraph starts after it.", fontsize=11)
    pdf = tmp_path / "pic.pdf"
    doc.save(pdf)
    blocks = extract.extract(pdf, tmp_path / "w", EXTRACT_CFG)
    kinds = [b["type"] for b in blocks]
    assert kinds.count("image") == 1
    image = next(b for b in blocks if b["type"] == "image")
    assert image.get("full_page") and (tmp_path / "w" / image["asset"]).exists()
    at = kinds.index("image")
    assert "continues on the next page" in blocks[at - 1]["text"] and "then" in blocks[at - 1]["text"]


def test_scene_break_picture_stays_in_place(tmp_path):
    """Звёздочка между абзацами остаётся между ними, а не уезжает в конец страницы и не рвёт абзац."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "The first scene ends right here with a full stop.", fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 34), 0)
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(290, 115, 298, 124), pixmap=pix)
    page.insert_text((50, 145), "The second scene begins and runs to the bottom of the page and", fontsize=11)
    doc.new_page().insert_text((50, 60), "goes on over here until it finally ends.", fontsize=11)
    pdf = tmp_path / "scene.pdf"
    doc.save(pdf)
    blocks = extract.extract(pdf, tmp_path / "w", {**EXTRACT_CFG, "min_image_pt": 6})
    assert [b["type"] for b in blocks] == ["paragraph", "image", "paragraph"]
    assert blocks[2]["text"].endswith("until it finally ends.") and abs(blocks[1]["width_pt"] - 8) < 0.5
