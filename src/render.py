"""Блоки + переводы → page.html → PDF (Playwright, Chromium)."""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

PT_PER_INCH = 72
PT_PER_MM = 72 / 25.4
PAGE_FORMATS_PT = {"A4": (595.28, 841.89), "A5": (419.53, 595.28), "Letter": (612.0, 792.0), "Legal": (612.0, 1008.0)}


def page_points(cfg_render, source_size) -> tuple[float, float]:
    """Размер страницы результата в пунктах: как у исходника или выбранный формат."""
    size = cfg_render["page_size"]
    if size == "auto":
        return float(source_size[0]), float(source_size[1])
    return PAGE_FORMATS_PT[size]


def write_html(blocks, title, work_dir, cfg_render, lang, source_size=(595.28, 841.89)) -> Path:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    html = env.get_template("template.html.j2").render(
        blocks=blocks,
        title=title,
        lang=lang,
        font_family=cfg_render["font_family"],
        font_size_pt=cfg_render["font_size_pt"],
        line_height=cfg_render["line_height"],
        margin_mm=cfg_render["margin_mm"],
        paragraph_spacing_em=cfg_render.get("paragraph_spacing_em", 0),
        text_indent_em=cfg_render.get("text_indent_em", 1.4),
        page=page_points(cfg_render, source_size),
    )
    path = Path(work_dir) / "page.html"
    path.write_text(html, encoding="utf-8")
    return path


def _page_options(cfg_render, source_size) -> dict:
    """Поля и номер страницы задаёт @page в шаблоне: у страниц с иллюстрацией нет ни полей, ни номера."""
    width, height = page_points(cfg_render, source_size)
    return {
        "print_background": True,
        "width": f"{width / PT_PER_INCH:.3f}in",
        "height": f"{height / PT_PER_INCH:.3f}in",
    }


def html_to_pdf(html_path: Path, out_path: Path, cfg_render, source_size):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            # Страница содержит текст из чужого PDF: скрипты не нужны.
            context = browser.new_context(java_script_enabled=False)
            page = context.new_page()
            page.goto(Path(html_path).resolve().as_uri(), wait_until="networkidle")
            page.pdf(path=str(tmp), **_page_options(cfg_render, source_size))
        finally:
            browser.close()
    tmp.replace(out_path)
    log.info("Готово: %s", out_path)
