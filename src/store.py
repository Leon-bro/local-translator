"""SQLite-состояние: файлы очереди и чанки.

Строка chunks — одна попытка перевода одного куска одной моделью одним промптом.
Ключ идемпотентности (file_hash, block_id, part, model, prompt_ver) соответствует плану,
part нужен потому, что длинные блоки режутся на несколько чанков.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

STATUSES = ["new", "extracted", "pass1_done", "pass2_done", "rendered"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_hash  TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'new',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode          TEXT,  -- выбранный вариант обработки
    rendered_mode TEXT   -- вариант, которым собран текущий PDF
);
CREATE TABLE IF NOT EXISTS chunks (
    file_hash  TEXT NOT NULL,
    block_id   INTEGER NOT NULL,
    part       INTEGER NOT NULL,
    model      TEXT NOT NULL,
    prompt_ver INTEGER NOT NULL,
    src        TEXT NOT NULL,
    src_hash   TEXT NOT NULL,
    dst        TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    flagged    INTEGER NOT NULL DEFAULT 0,
    reasons    TEXT,
    truncated  INTEGER NOT NULL DEFAULT 0,
    seconds    REAL,
    terms_hash TEXT,  -- какие термины глоссария были в промпте
    PRIMARY KEY (file_hash, block_id, part, model, prompt_ver)
);
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


class Store:
    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(files)")}
        for column in ("mode", "rendered_mode"):
            if column not in columns:  # база, созданная до появления режимов
                self.db.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
        if "terms_hash" not in {r["name"] for r in self.db.execute("PRAGMA table_info(chunks)")}:
            self.db.execute("ALTER TABLE chunks ADD COLUMN terms_hash TEXT")
        self.db.commit()

    def close(self):
        self.db.close()

    # --- файлы -----------------------------------------------------------
    def add_file(self, path: Path, mode: str | None = None) -> str:
        """Новому файлу назначает mode. У файла, который уже в очереди, режим не меняется."""
        fh = file_hash(path)
        self.db.execute(
            "INSERT INTO files(file_hash, path, name, mode) VALUES (?,?,?,?) "
            "ON CONFLICT(file_hash) DO UPDATE SET path=excluded.path, name=excluded.name",
            (fh, str(path), Path(path).name, mode),
        )
        self.db.commit()
        return fh

    def files(self, statuses=None) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM files ORDER BY rowid").fetchall()
        return [r for r in rows if statuses is None or r["status"] in statuses]

    def get_file(self, fh: str):
        return self.db.execute("SELECT * FROM files WHERE file_hash=?", (fh,)).fetchone()

    def set_status(self, fh: str, status: str):
        assert status in STATUSES, status
        self.db.execute(
            "UPDATE files SET status=?, updated_at=datetime('now') WHERE file_hash=?", (status, fh)
        )
        self.db.commit()

    def set_mode(self, fh: str, mode: str):
        self.db.execute("UPDATE files SET mode=? WHERE file_hash=?", (mode, fh))
        self.db.commit()

    def set_rendered_mode(self, fh: str, mode: str):
        self.db.execute("UPDATE files SET rendered_mode=? WHERE file_hash=?", (mode, fh))
        self.db.commit()

    def backfill_modes(self, default: str):
        """Файлам из старой базы фиксирует режим: потом смена режима по умолчанию их не затронет."""
        self.db.execute("UPDATE files SET rendered_mode=? WHERE rendered_mode IS NULL AND status='rendered'", (default,))
        self.db.execute("UPDATE files SET mode=? WHERE mode IS NULL", (default,))
        self.db.commit()

    def remove_file(self, fh: str):
        self.db.execute("DELETE FROM chunks WHERE file_hash=?", (fh,))
        self.db.execute("DELETE FROM files WHERE file_hash=?", (fh,))
        self.db.commit()

    # --- чанки -----------------------------------------------------------
    def upsert_chunk(self, fh, block_id, part, model, prompt_ver, src):
        """Создаёт pending-строку. Готовый перевод сбрасывается, только если изменился исходник."""
        self.db.execute(
            """INSERT INTO chunks(file_hash, block_id, part, model, prompt_ver, src, src_hash)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(file_hash, block_id, part, model, prompt_ver) DO UPDATE SET
                 src=excluded.src, src_hash=excluded.src_hash,
                 dst=NULL, status='pending', flagged=0, reasons=NULL, truncated=0, seconds=NULL
               WHERE chunks.src_hash <> excluded.src_hash""",
            (fh, block_id, part, model, prompt_ver, src, sha(src)),
        )

    def reconcile_chunks(self, fh, chunks: list[tuple[int, int, str]]):
        """Приводит строки к новому разбиению книги. Готовый перевод идёт за своим текстом, а не за номером блока:
        после правки извлечения номера сдвигаются, и иначе пришлось бы переводить книгу заново."""
        wanted = {(b, p): sha(text) for b, p, text in chunks}
        rows = self.db.execute("SELECT * FROM chunks WHERE file_hash=?", (fh,)).fetchall()
        stale = [r for r in rows if wanted.get((r["block_id"], r["part"])) != r["src_hash"]]
        if not stale:
            return

        finished: dict[str, list] = {}
        for row in rows:
            if row["status"] == "done":
                finished.setdefault(row["src_hash"], []).append(row)
        for row in stale:
            self.db.execute(
                "DELETE FROM chunks WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?",
                (fh, row["block_id"], row["part"], row["model"], row["prompt_ver"]),
            )
        for block_id, part, text in chunks:
            for row in finished.get(sha(text), []):
                self.db.execute(
                    """INSERT OR IGNORE INTO chunks(file_hash, block_id, part, model, prompt_ver, src, src_hash,
                                                    dst, status, flagged, reasons, truncated, seconds, terms_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fh, block_id, part, row["model"], row["prompt_ver"], text, row["src_hash"], row["dst"],
                     row["status"], row["flagged"], row["reasons"], row["truncated"], row["seconds"], row["terms_hash"]),
                )

    def commit(self):
        self.db.commit()

    def rows(self, fh, model, prompt_ver, status=None) -> list[sqlite3.Row]:
        q = "SELECT * FROM chunks WHERE file_hash=? AND model=? AND prompt_ver=?"
        args = [fh, model, prompt_ver]
        if status:
            q += " AND status=?"
            args.append(status)
        return self.db.execute(q + " ORDER BY block_id, part", args).fetchall()

    def save_translation(self, row, dst, truncated, seconds, terms_hash=None):
        self.db.execute(
            """UPDATE chunks SET dst=?, status='done', truncated=?, seconds=?, terms_hash=?
               WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?""",
            (dst, int(truncated), seconds, terms_hash, row["file_hash"], row["block_id"], row["part"],
             row["model"], row["prompt_ver"]),
        )
        self.db.commit()

    def retry(self, row):
        """Ставит кусок на повторный перевод: старый текст заменится, когда появится новый."""
        self.db.execute(
            """UPDATE chunks SET status='pending'
               WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?""",
            (row["file_hash"], row["block_id"], row["part"], row["model"], row["prompt_ver"]),
        )

    def mark_error(self, row, message):
        self.db.execute(
            """UPDATE chunks SET status='error', reasons=?
               WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?""",
            (message[:500], row["file_hash"], row["block_id"], row["part"], row["model"],
             row["prompt_ver"]),
        )
        self.db.commit()

    def set_flag(self, row, reasons: list[str]):
        self.db.execute(
            """UPDATE chunks SET flagged=?, reasons=?
               WHERE file_hash=? AND block_id=? AND part=? AND model=? AND prompt_ver=?""",
            (int(bool(reasons)), "; ".join(reasons) or None, row["file_hash"], row["block_id"],
             row["part"], row["model"], row["prompt_ver"]),
        )
