"""Конвейер и командная строка: run, resume, rerender, preview, status."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import yaml

import json

from . import characters, chunk, context, extract, glossary_import, llm, names, render, validate
from .store import Store, file_hash, sha

log = logging.getLogger("translator")
ROOT = Path(__file__).resolve().parent.parent


class StaleTranslation(RuntimeError):
    """Перевода для текущих модели и промпта нет: PDF собрался бы на исходном языке."""


class Pipeline:
    def __init__(self, cfg: dict, root: Path = ROOT, client=None):
        self.cfg = cfg
        self.root = root
        paths = cfg["paths"]
        self.input_dir = root / paths["input_dir"]
        self.output_dir = root / paths["output_dir"]
        self.work_dir = root / paths["work_dir"]
        self.store = Store(root / paths["db"])
        self.paths = paths
        self.groups = context.load_groups(root)
        self._contexts: dict[str, context.Context] = {}
        self.llm_cfg = cfg["llm"]
        self.keep_names = self.llm_cfg.get("keep_names", False)
        self._kept: dict[str, list[str]] = {}
        self._genders: dict[str, dict[str, str]] = {}
        self.client = client or llm.Ollama(self.llm_cfg)
        self.ver = self.llm_cfg["prompt_ver"]
        self.modes = {m["id"]: m for m in self.llm_cfg["modes"]}
        self.default_mode = self.llm_cfg["default_mode"]
        if self.default_mode not in self.modes:
            raise SystemExit(f"config.yaml: default_mode {self.default_mode} нет среди modes")
        self.store.backfill_modes(self.default_mode)
        self.changed: set[str] = set()

    # --- контекст группы книг ----------------------------------------------
    def context(self, fh: str) -> context.Context:
        group = context.group_of(self.groups, fh)
        if group not in self._contexts:
            self._contexts[group] = context.load(self.root, self.paths, group, self.keep_names)
        return self._contexts[group]

    # --- варианты обработки ------------------------------------------------
    def add(self, path: Path, mode: str | None = None) -> str:
        fh = self.store.add_file(path, mode or self.default_mode)
        if mode:
            self.store.set_mode(fh, mode)
        return fh

    def mode_id(self, row) -> str:
        return row["mode"] if row["mode"] in self.modes else self.default_mode

    def models(self, fh: str) -> tuple[str, str]:
        mode = self.modes[self.mode_id(self.store.get_file(fh))]
        return mode["pass1_model"], mode["pass2_model"]

    def extract_outdated(self, row) -> bool:
        """Книга разобрана старой версией извлечения. Без исходного файла остаётся прежний разбор."""
        path = self.work_dir / row["file_hash"] / "blocks.json"
        if not path.exists():
            return True
        version = extract.load_blocks(self.work_dir / row["file_hash"]).get("extract_version", 1)
        return version != extract.EXTRACT_VERSION and Path(row["path"]).exists()

    def mark_outdated(self):
        """Готовые книги со старым разбором снова попадают в очередь «Перевести очередь»."""
        for row in self.store.files({"rendered"}):
            if self.extract_outdated(row):
                self.store.set_status(row["file_hash"], "extracted")

    def _book_texts(self, fh: str) -> list[str]:
        return [b["text"] for b in extract.load_blocks(self.work_dir / fh)["blocks"] if b.get("text")]

    def kept_names(self, fh: str) -> list[str]:
        """Что модель не должна увидеть: имена не из глоссария и термины вида «Vortex Crash = Vortex Crash»."""
        if not self.keep_names:
            return []
        if fh not in self._kept:
            ctx = self.context(fh)
            same = [s for s, d in ctx.glossary if s == d]
            self._kept[fh] = same + names.find_names(self._book_texts(fh), ctx.glossary + ctx.hints, ctx.plain)
        return self._kept[fh]

    def _name_pool(self, fh: str) -> list[str]:
        """Всё, что может оказаться именем персонажа: и найденное в книге, и записанное в глоссарии."""
        ctx = self.context(fh)
        known = ctx.glossary + ctx.hints
        return names.find_names(self._book_texts(fh), known, ctx.plain) + [s for s, _ in known if s[:1].isupper()]

    def genders(self, fh: str) -> dict[str, str]:
        if fh not in self._genders:
            self._genders[fh] = characters.effective(self._book_texts(fh), self._name_pool(fh), self.context(fh).decisions)
        return self._genders[fh]

    def names_report(self, fh: str) -> dict:
        ctx = self.context(fh)
        texts = self._book_texts(fh)
        whole = "\n".join(texts)
        return {"candidates": names.candidates(texts, ctx.glossary + ctx.hints, ctx.plain),
                "known": [{"source": s, "target": d} for s, d in llm.glossary_for(whole, ctx.glossary)],
                "plain": ctx.plain, "keepNames": self.keep_names, "group": ctx.group,
                "characters": characters.report(texts, self._name_pool(fh), ctx.decisions)}

    def needs_work(self, row) -> bool:
        """Не завершён, устарел разбор, собран другим режимом или часть кусков переводили с другими настройками."""
        fh = row["file_hash"]
        if row["status"] != "rendered" or row["rendered_mode"] != self.mode_id(row) or self.extract_outdated(row):
            return True
        known = self.store.db.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT block_id, part FROM chunks WHERE file_hash=?)", (fh,)
        ).fetchone()[0]
        return len(self.store.rows(fh, self.models(fh)[0], self.ver)) < known

    # --- стадии ----------------------------------------------------------
    def stage_extract(self, fh: str, force=False):
        row = self.store.get_file(fh)
        work = self.work_dir / fh
        outdated = self.extract_outdated(row)
        if not force and row["status"] != "new" and not outdated:
            return
        extract.extract(row["path"], work, self.cfg["extract"])
        self._kept.pop(fh, None)
        self._genders.pop(fh, None)
        if outdated:  # текст мог не измениться, но блоки разбиты иначе: PDF нужно собрать заново
            self.changed.add(fh)
        if row["status"] == "new":
            self.store.set_status(fh, "extracted")

    def stage_chunk(self, fh: str):
        blocks = extract.load_blocks(self.work_dir / fh)["blocks"]
        chunks = chunk.build_chunks(blocks, self.cfg["chunk"]["max_tokens"])
        m1, _ = self.models(fh)
        self.store.reconcile_chunks(fh, chunks)
        for block_id, part, text in chunks:
            self.store.upsert_chunk(fh, block_id, part, m1, self.ver, text)
        self.store.commit()
        if self._unfinished(fh, m1):
            self.store.set_status(fh, "extracted")

    def _unfinished(self, fh, model) -> bool:
        return any(r["status"] != "done" for r in self.store.rows(fh, model, self.ver))

    def _previous_translation(self, fh, row, done: dict) -> str | None:
        key = (row["block_id"], row["part"])
        earlier = [k for k in done if k < key]
        if not earlier:
            return None
        text = done[max(earlier)]
        limit = self.cfg["chunk"]["context_chars"]
        return text[-limit:] if len(text) > limit else text

    def _translate_pending(self, fh: str, model: str) -> int:
        """Переводит pending и error. Прогресс сохраняется после каждого чанка, так что прерывание безопасно."""
        rows = [r for r in self.store.rows(fh, model, self.ver) if r["status"] != "done"]
        if not rows:
            return 0
        # Контекст берётся из лучшего перевода: на втором проходе это и результаты первого.
        m1, _ = self.models(fh)
        done = {(r["block_id"], r["part"]): r["dst"] for r in self.store.rows(fh, m1, self.ver, "done")}
        if model != m1:
            done.update({(r["block_id"], r["part"]): r["dst"] for r in self.store.rows(fh, model, self.ver, "done")})

        name = self.store.get_file(fh)["name"]
        ctx = self.context(fh)
        started = time.perf_counter()
        for i, row in enumerate(rows, 1):
            masked, tokens = names.mask(row["src"], self.kept_names(fh))
            prompt = llm.build_prompt(
                masked, self.cfg["source_lang"], self.cfg["target_lang"],
                characters.annotate(llm.glossary_for(masked, ctx.glossary + ctx.hints), tokens, self.genders(fh)),
                self._previous_translation(fh, row, done),
            )
            result = self._call(model, prompt, row)
            if result is None:
                continue
            text = names.restore(result.text, tokens)
            self.store.save_translation(row, text, result.truncated, result.seconds, self._terms_hash(fh, row["src"]))
            done[(row["block_id"], row["part"])] = text
            self.changed.add(fh)
            log.info("%s [%s] %d/%d блок %d.%d: %.1f с", name, model, i, len(rows),
                     row["block_id"], row["part"], result.seconds)
        log.info("%s [%s]: %d чанков за %.0f с", name, model, len(rows), time.perf_counter() - started)
        return len(rows)

    def _required_terms(self, fh: str) -> list[tuple[str, str]]:
        return self.context(fh).glossary + [(n, n) for n in self.kept_names(fh)]

    def _terms_hash(self, fh: str, src: str) -> str:
        terms = self._required_terms(fh) + self.context(fh).hints
        genders = self.genders(fh)
        # Пол входит в отпечаток только если он известен: у остальных кусков отпечаток прежний.
        return sha("\n".join(f"{s}={d}" + (f"/{g}" if (g := characters.gender_of(s, genders)) else "")
                             for s, d in llm.glossary_for(src, terms)))

    def retranslate_gendered(self, fh: str) -> int:
        """Ставит на повтор готовые куски с персонажами, переведённые без их текущего пола.

        Повторный вызов ничего не добавляет: у переведённого заново куска отпечаток уже совпадает.
        Старый текст остаётся видимым, пока не появится новый.
        """
        genders, terms = self.genders(fh), self._required_terms(fh)
        count = 0
        for model in dict.fromkeys(self.models(fh)):
            for row in self.store.rows(fh, model, self.ver, "done"):
                people = llm.glossary_for(row["src"], terms)
                has_gender = any(s[:1].isupper() and characters.gender_of(s, genders) for s, _ in people)
                if has_gender and row["terms_hash"] != self._terms_hash(fh, row["src"]):
                    self.store.retry(row)
                    count += 1
        self.store.commit()
        if count:
            self.store.set_status(fh, "extracted")
        return count

    def _call(self, model, prompt, row):
        attempts = self.llm_cfg["retries"] + 1
        for attempt in range(1, attempts + 1):
            try:
                return self.client.translate(model, prompt, row["src"])
            except llm.OllamaUnavailable:
                raise
            except Exception as exc:
                log.warning("Блок %d.%d, попытка %d/%d: %s", row["block_id"], row["part"], attempt, attempts, exc)
                if attempt == attempts:
                    self.store.mark_error(row, str(exc))
        return None

    def stage_pass1(self, fh: str):
        m1, _ = self.models(fh)
        self._translate_pending(fh, m1)
        if not self._unfinished(fh, m1) and self.store.get_file(fh)["status"] == "extracted":
            self.store.set_status(fh, "pass1_done")

    def _validate_rows(self, fh, model):
        cfg = self.cfg["validate"]
        rows = self.store.rows(fh, model, self.ver, "done")
        terms = self._required_terms(fh)
        genders, plain = self.genders(fh), self.context(fh).plain
        for row in rows:
            self.store.set_flag(row, validate.check(row["src"], row["dst"], row["truncated"], terms, cfg, plain, genders))
        self.store.commit()
        return rows

    def stage_validate(self, fh: str):
        """Помеченные чанки получают строки для 12B. Запускается и повторно: новый глоссарий может пометить новые."""
        m1, m2 = self.models(fh)
        rows = self._validate_rows(fh, m1)
        if m2 != m1:
            self._validate_rows(fh, m2)  # глоссарий мог измениться с прошлого запуска
        flagged = self.store.db.execute(
            "SELECT * FROM chunks WHERE file_hash=? AND model=? AND prompt_ver=? AND flagged=1 ORDER BY block_id, part",
            (fh, m1, self.ver),
        ).fetchall()
        for row in flagged:
            log.info("Помечен блок %d.%d: %s", row["block_id"], row["part"], row["reasons"])
            self.store.upsert_chunk(fh, row["block_id"], row["part"], m2, self.ver, row["src"])
        keep = {(r["block_id"], r["part"]) for r in flagged}
        # Последняя модель уже видела этот кусок, но с другими терминами: без повтора новый глоссарий не применится.
        # Если термины те же, повторять незачем: иначе упрямый кусок переводился бы при каждом запуске.
        for row in self.store.rows(fh, m2, self.ver, "done"):
            if ((row["block_id"], row["part"]) in keep and validate.fixable_by_terms(row["reasons"])
                    and row["terms_hash"] != self._terms_hash(fh, row["src"])):
                self.store.retry(row)
        for row in self.store.rows(fh, m2, self.ver, "pending"):
            if (row["block_id"], row["part"]) not in keep:
                self.store.db.execute(
                    "DELETE FROM chunks WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?",
                    (fh, row["block_id"], row["part"], m2, self.ver),
                )
        self.store.commit()
        if self._unfinished(fh, m2) and self.store.get_file(fh)["status"] in ("pass2_done", "rendered"):
            self.store.set_status(fh, "pass1_done")
        log.info("%s: помечено %d из %d (%.0f %%)", self.store.get_file(fh)["name"], len(flagged), len(rows),
                 100 * len(flagged) / max(1, len(rows)))

    def stage_pass2(self, fh: str):
        _, m2 = self.models(fh)
        self._translate_pending(fh, m2)
        self._validate_rows(fh, m2)
        if not self._unfinished(fh, m2) and self.store.get_file(fh)["status"] == "pass1_done":
            self.store.set_status(fh, "pass2_done")

    def best_translation(self, fh: str) -> tuple[dict, dict]:
        """(block_id, part) → лучший перевод. 12B заменяет 4B, если не оказался хуже по эвристикам."""
        best = {}
        counts = {"pass1": 0, "pass2": 0, "still_flagged": 0}
        m1, m2 = self.models(fh)
        first = {(r["block_id"], r["part"]): r for r in self.store.rows(fh, m1, self.ver, "done")}
        second = {(r["block_id"], r["part"]): r for r in self.store.rows(fh, m2, self.ver, "done")} if m2 != m1 else {}
        for key, row in first.items():
            chosen = row
            other = second.get(key)
            if other is not None and row["flagged"]:
                if _reason_count(other) <= _reason_count(row):
                    chosen = other
            counts["pass2" if chosen is not row else "pass1"] += 1
            if chosen["flagged"]:
                counts["still_flagged"] += 1
                log.warning("Блок %d.%d остался помеченным: %s", key[0], key[1], chosen["reasons"])
            best[key] = chosen["dst"]
        return best, counts

    def stage_render(self, fh: str):
        row = self.store.get_file(fh)
        best, counts = self.best_translation(fh)
        data = extract.load_blocks(self.work_dir / fh)
        expected = chunk.build_chunks(data["blocks"], self.cfg["chunk"]["max_tokens"])
        if len(best) != len(expected):
            raise StaleTranslation(
                f"{row['name']}: для текущих настроек переведено {len(best)} из {len(expected)} чанков. "
                "Сначала запустите перевод."
            )
        blocks = merge_translations(data["blocks"], best)
        out = self.output_dir / f"{Path(row['name']).stem}.pdf"
        html = render.write_html(blocks, Path(row["name"]).stem, self.work_dir / fh, self.cfg["render"],
                                 self.cfg["target_lang"]["code"], data["page_size"])
        render.html_to_pdf(html, out, self.cfg["render"], data["page_size"])
        self.store.set_status(fh, "rendered")
        self.store.set_rendered_mode(fh, self.mode_id(row))
        total = counts["pass1"] + counts["pass2"]
        log.info("%s: чанков %d, из них второй моделью %d (%.0f %%), осталось помеченных %d",
                 row["name"], total, counts["pass2"], 100 * counts["pass2"] / max(1, total), counts["still_flagged"])
        return out

    def preview(self, path: Path) -> Path:
        """Цепочка без LLM: проверка извлечения и рендера на неудобных PDF."""
        work = self.work_dir / file_hash(path)
        extract.extract(path, work, self.cfg["extract"])
        data = extract.load_blocks(work)
        html = render.write_html(data["blocks"], path.stem, work, self.cfg["render"], self.cfg["source_lang"]["code"],
                                 data["page_size"])
        out = self.output_dir / f"{path.stem}.preview.pdf"
        render.html_to_pdf(html, out, self.cfg["render"], data["page_size"])
        return out

    def remove(self, fh: str):
        """Удаляет файл из очереди с переводами. Готовый PDF в out/ не трогает."""
        row = self.store.get_file(fh)
        if row is None:
            raise SystemExit(f"Нет такого файла в очереди: {fh}")
        path = Path(row["path"])
        self.store.remove_file(fh)
        shutil.rmtree(self.work_dir / fh, ignore_errors=True)
        # Копия в in/ снова попала бы в очередь при следующем сканировании.
        if path.resolve().parent == self.input_dir.resolve():
            path.unlink(missing_ok=True)
        log.info("Удалён из очереди: %s", row["name"])

    # --- очередь ---------------------------------------------------------
    def process(self, hashes: list[str]) -> int:
        """Стадии идут по всей очереди: сначала все файлы через 4B, потом все помеченные через 12B."""
        started = time.perf_counter()
        self.changed = set()
        ready = []
        failed = 0
        for fh in hashes:
            try:
                self.stage_extract(fh)
                self.stage_chunk(fh)
                ready.append(fh)
            except extract.ExtractError as exc:
                failed += 1
                log.error("%s: %s", self.store.get_file(fh)["name"], exc)

        # Быстрому режиму не нужна 12B: требуем только модели книг из этой очереди.
        self.client.check(sorted({m for fh in ready for m in self.models(fh)}))
        for fh in ready:
            self.stage_pass1(fh)
        for fh in ready:
            if self.store.get_file(fh)["status"] != "extracted":
                self.stage_validate(fh)
        for fh in ready:
            if self.store.get_file(fh)["status"] == "pass1_done":
                self.stage_pass2(fh)
        if self.client.loaded:
            self.client.unload(self.client.loaded)
            self.client.loaded = None

        for fh in ready:
            row = self.store.get_file(fh)
            out = self.output_dir / f"{Path(row['name']).stem}.pdf"
            outdated = fh in self.changed or not out.exists() or row["rendered_mode"] != self.mode_id(row)
            if row["status"] == "pass2_done" or (row["status"] == "rendered" and outdated):
                self.stage_render(fh)
            elif row["status"] != "rendered":
                failed += 1
                log.error("%s: остались непереведённые чанки (статус %s). Повторите: resume", row["name"], row["status"])
        log.info("Очередь: файлов %d, с ошибками %d, время %.0f с", len(hashes), failed, time.perf_counter() - started)
        return failed


def merge_translations(blocks: list[dict], best: dict) -> list[dict]:
    """Склеивает части блока. Блок без перевода (например, только цифры) остаётся как в исходнике."""
    parts: dict[int, list[tuple[int, str]]] = {}
    for (block_id, part), text in best.items():
        parts.setdefault(block_id, []).append((part, text))
    out = []
    for block in blocks:
        block = dict(block)
        if block["id"] in parts:
            block["text"] = " ".join(t for _, t in sorted(parts[block["id"]]))
        out.append(block)
    return out


def _reason_count(row) -> int:
    return len(row["reasons"].split("; ")) if row["reasons"] else 0


def load_config(root: Path = ROOT) -> dict:
    return yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))


def setup_logging(log_file: Path, verbose: bool):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def _find_pdfs(pipeline: Pipeline, paths: list[str]) -> list[Path]:
    if paths:
        found = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                found.extend(sorted(path.glob("*.pdf")))
            elif path.is_file():
                found.append(path)
            else:
                raise SystemExit(f"Файл не найден: {raw}")
        return [p.resolve() for p in found]
    pipeline.input_dir.mkdir(exist_ok=True)
    return sorted(pipeline.input_dir.glob("*.pdf"))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="translator", description="Локальный двухпроходный переводчик PDF")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="полный цикл для файлов (по умолчанию все PDF из in/)")
    p_run.add_argument("paths", nargs="*")
    p_run.add_argument("--mode", help="вариант обработки из config.yaml: fast, balanced, quality")
    sub.add_parser("resume", help="продолжить незавершённые файлы")
    p_rerender = sub.add_parser("rerender", help="пересобрать PDF без перевода (после правки CSS или шрифта)")
    p_rerender.add_argument("paths", nargs="*")
    p_preview = sub.add_parser("preview", help="извлечь и отрисовать без перевода")
    p_preview.add_argument("paths", nargs="+")
    sub.add_parser("status", help="состояние очереди")
    p_add = sub.add_parser("add", help="поставить файлы в очередь, ничего не запуская (по умолчанию все PDF из in/)")
    p_add.add_argument("paths", nargs="*")
    p_mode = sub.add_parser("set-mode", help="выбрать вариант обработки книги, ничего не запуская")
    p_mode.add_argument("file_hash")
    p_mode.add_argument("mode")
    p_import = sub.add_parser("import-glossary", help="добавить термины из .xlsx в глоссарий и подсказки")
    p_import.add_argument("file")
    p_import.add_argument("--group", default=context.DEFAULT, help="группа книг, чей контекст дополняется")
    p_names = sub.add_parser("names", help="имена книги, которых нет в глоссарии (JSON)")
    p_names.add_argument("file_hash")
    p_gender = sub.add_parser("retranslate-gender", help="поставить на повтор готовые куски с персонажами известного пола")
    p_gender.add_argument("file_hash")
    p_extract = sub.add_parser("extract", help="разобрать книгу без перевода")
    p_extract.add_argument("file_hash")
    p_remove = sub.add_parser("remove", help="убрать файл из очереди вместе с его переводами")
    p_remove.add_argument("file_hash")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(ROOT / cfg["paths"]["log_file"], args.verbose)
    pipeline = Pipeline(cfg)
    try:
        if args.command == "status":
            for row in pipeline.store.files():
                print(f"{row['status']:<12} {row['file_hash']}  {row['name']}")
            return 0

        if args.command == "add":
            pipeline.mark_outdated()
            for path in _find_pdfs(pipeline, args.paths):
                print(pipeline.add(path), path.name)
            return 0

        if args.command == "import-glossary":
            files = context.files_for(ROOT, cfg["paths"], args.group)
            files["glossary"].parent.mkdir(parents=True, exist_ok=True)
            try:
                result = glossary_import.import_file(args.file, files["glossary"], files["hints"])
            except (OSError, ValueError, KeyError) as exc:
                log.error("Не удалось прочитать файл: %s", exc)
                return 1
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command == "names":
            print(json.dumps(pipeline.names_report(args.file_hash), ensure_ascii=False))
            return 0

        if args.command == "retranslate-gender":
            print(pipeline.retranslate_gendered(args.file_hash))
            return 0

        if args.command == "extract":
            if pipeline.store.get_file(args.file_hash) is None:
                log.error("Нет такой книги: %s", args.file_hash)
                return 1
            try:
                pipeline.stage_extract(args.file_hash)
            except extract.ExtractError as exc:
                log.error("%s", exc)
                return 1
            return 0

        if args.command == "set-mode":
            if args.mode not in pipeline.modes or pipeline.store.get_file(args.file_hash) is None:
                log.error("Неверная книга или режим. Режимы: %s", ", ".join(pipeline.modes))
                return 1
            pipeline.store.set_mode(args.file_hash, args.mode)
            return 0

        if args.command == "remove":
            pipeline.remove(args.file_hash)
            return 0

        if args.command == "preview":
            for path in _find_pdfs(pipeline, args.paths):
                print(pipeline.preview(path))
            return 0

        if args.command == "rerender":
            wanted = {pipeline.add(p) for p in _find_pdfs(pipeline, args.paths)} if args.paths else None
            done = 0
            for row in pipeline.store.files({"pass2_done", "rendered"}):
                if wanted is None or row["file_hash"] in wanted:
                    pipeline.stage_render(row["file_hash"])
                    done += 1
            if wanted and done < len(wanted):
                log.error("Часть файлов ещё не переведена: сначала run или resume")
                return 1
            return 0

        if args.command == "resume":
            hashes = [r["file_hash"] for r in pipeline.store.files() if pipeline.needs_work(r)]
        else:
            pdfs = _find_pdfs(pipeline, args.paths)
            if args.mode and args.mode not in pipeline.modes:
                raise SystemExit(f"Неизвестный режим: {args.mode}. Режимы: {', '.join(pipeline.modes)}")
            hashes = [pipeline.add(p, args.mode) for p in pdfs]
        if not hashes:
            print("Очередь пуста")
            return 0
        return 1 if pipeline.process(hashes) else 0
    except llm.OllamaUnavailable as exc:
        log.error("%s", exc)
        return 2
    except StaleTranslation as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Прервано. Прогресс сохранён, продолжить: resume")
        return 130
    finally:
        pipeline.store.close()


if __name__ == "__main__":
    sys.exit(main())
