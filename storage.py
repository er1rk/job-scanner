"""
SQLite storage for every scored offer and every run.

Why: the e-mail only shows offers above the threshold. Keeping *all* scored offers
(with the extracted fields) turns the scanner into a dataset you can analyse in
Power BI / Excel: market volume per category, salary ranges, most common skill gaps.

Tables
  offers  - one row per scored offer (also below the threshold)
  runs    - one row per scanner run (counts, model used, duration)

The file lives in data/jobs.db (git-ignored, persisted between GitHub Actions runs via cache).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS offers (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL DEFAULT 'linkedin',
    first_seen         TEXT NOT NULL,          -- ISO timestamp (UTC)
    title              TEXT,
    company            TEXT,
    location           TEXT,
    url                TEXT,
    search_keyword     TEXT,
    -- scoring
    score              INTEGER,
    verdict            TEXT,
    seniority          TEXT,
    one_line_reason    TEXT,
    strengths          TEXT,                   -- JSON array
    gaps               TEXT,                   -- JSON array
    notified           INTEGER DEFAULT 0,      -- 1 = included in the e-mail
    -- extracted facts
    role_category      TEXT,
    industry           TEXT,
    work_mode          TEXT,
    contract_types     TEXT,                   -- JSON array
    salary_min         REAL,
    salary_max         REAL,
    salary_currency    TEXT,
    salary_period      TEXT,
    salary_type        TEXT,                   -- gross / net / unknown
    salary_min_pln_month REAL,                 -- normalised for comparisons
    salary_max_pln_month REAL,
    years_required     REAL,
    english_level      TEXT,
    polish_required    INTEGER,
    required_tools     TEXT,                   -- JSON array
    nice_to_have_tools TEXT,                   -- JSON array
    -- bookkeeping
    model              TEXT,
    description_z      BLOB,                   -- zlib-compressed description (for later re-scoring / evaluation)
    -- application tracking (filled in manually or by later features)
    user_label         TEXT,                   -- e.g. good / bad / applied
    status             TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT,
    finished_at    TEXT,
    searches       INTEGER,
    cards_found    INTEGER,
    filtered_title INTEGER,
    new_offers     INTEGER,
    scored         INTEGER,
    failed         INTEGER,
    notified       INTEGER,
    model          TEXT
);
CREATE TABLE IF NOT EXISTS seen (                -- ids skipped without scoring (e.g. empty description)
    id TEXT PRIMARY KEY
);
"""

LIST_FIELDS = ("strengths", "gaps", "contract_types", "required_tools", "nice_to_have_tools")

# Columns exported to CSV (description is left out - too long for a spreadsheet)
EXPORT_COLUMNS = [
    "id", "source", "first_seen", "title", "company", "location", "url", "search_keyword",
    "score", "verdict", "seniority", "notified", "one_line_reason", "strengths", "gaps",
    "role_category", "industry", "work_mode", "contract_types",
    "salary_min", "salary_max", "salary_currency", "salary_period", "salary_type",
    "salary_min_pln_month", "salary_max_pln_month",
    "years_required", "english_level", "polish_required", "required_tools", "nice_to_have_tools",
    "model", "user_label", "status",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA_SQL)
        self.con.commit()

    # ------------------------------------------------------------ seen ids
    def seen_ids(self) -> set[str]:
        ids = {r[0] for r in self.con.execute("SELECT id FROM offers")}
        ids |= {r[0] for r in self.con.execute("SELECT id FROM seen")}
        return ids

    def mark_seen(self, offer_id: str) -> None:
        self.con.execute("INSERT OR IGNORE INTO seen(id) VALUES (?)", (offer_id,))
        self.con.commit()

    def import_legacy_seen(self, seen_file: Path) -> int:
        """One-off migration from the old data/seen.json so nothing gets scored twice."""
        if not seen_file.exists():
            return 0
        ids = json.loads(seen_file.read_text())
        self.con.executemany("INSERT OR IGNORE INTO seen(id) VALUES (?)", [(i,) for i in ids])
        self.con.commit()
        seen_file.rename(seen_file.with_suffix(".json.migrated"))
        return len(ids)

    # ------------------------------------------------------------ offers
    def save_offer(self, row: dict, description: str = "") -> None:
        data = {k: row.get(k) for k in EXPORT_COLUMNS if k in row}
        for f in LIST_FIELDS:
            if f in data and data[f] is not None:
                data[f] = json.dumps(data[f], ensure_ascii=False)
        if "polish_required" in data and data["polish_required"] is not None:
            data["polish_required"] = int(bool(data["polish_required"]))
        data.setdefault("first_seen", now_iso())
        if description:
            data["description_z"] = zlib.compress(description.encode("utf-8"))
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        # re-scoring an offer updates it but keeps when it was first seen and any manual labels
        keep = {"id", "first_seen", "user_label", "status", "notified"}
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in keep)
        self.con.execute(
            f"INSERT INTO offers ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
            list(data.values()))
        self.con.commit()

    def mark_notified(self, ids: list[str]) -> None:
        self.con.executemany("UPDATE offers SET notified = 1 WHERE id = ?", [(i,) for i in ids])
        self.con.commit()

    def description(self, offer_id: str) -> str:
        r = self.con.execute("SELECT description_z FROM offers WHERE id = ?", (offer_id,)).fetchone()
        return zlib.decompress(r[0]).decode("utf-8") if r and r[0] else ""

    # ------------------------------------------------------------ runs
    def save_run(self, stats: dict) -> None:
        cols = ", ".join(stats)
        marks = ", ".join("?" for _ in stats)
        self.con.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", list(stats.values()))
        self.con.commit()

    # ------------------------------------------------------------ export
    def export_csv(self) -> str:
        """All offers as CSV (UTF-8 with BOM so Excel shows Polish characters correctly).
        List fields are joined with ' | ' so they are easy to split in Power Query."""
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf)
        w.writerow(EXPORT_COLUMNS)
        for r in self.con.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM offers ORDER BY first_seen"):
            out = []
            for col in EXPORT_COLUMNS:
                v = r[col]
                if col in LIST_FIELDS and v:
                    v = " | ".join(json.loads(v))
                out.append("" if v is None else v)
            w.writerow(out)
        return buf.getvalue()

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]

    def close(self) -> None:
        self.con.close()
