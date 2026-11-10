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
import re
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
CREATE TABLE IF NOT EXISTS queue (               -- offers found but not scored yet (over the per-run cap, or failed)
    id        TEXT PRIMARY KEY,
    offer     TEXT NOT NULL,                     -- JSON: title, company, location, url, search_keyword
    queued_at TEXT NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0         -- failed scoring attempts
);
CREATE TABLE IF NOT EXISTS triaged (             -- offers skipped before scoring, kept for review (reason column)
    id         TEXT PRIMARY KEY,
    source     TEXT,
    title      TEXT,
    company    TEXT,
    location   TEXT,
    url        TEXT,
    triaged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (             -- weekly reports sent (so a backup schedule doesn't send twice)
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescued (             -- skipped offers marked "interested" in the app, already re-queued
    id         TEXT PRIMARY KEY,
    rescued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (                -- ids skipped without scoring (e.g. empty description)
    id TEXT PRIMARY KEY
);
"""

QUEUE_FIELDS = ("id", "source", "title", "company", "location", "url", "search_keyword",
                "posted", "extra", "fingerprint")

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
    "score_requirements", "score_experience", "score_education", "score_languages", "score_extras",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA_SQL)
        # columns added after the first release (CREATE TABLE IF NOT EXISTS doesn't add them to old files)
        for table, cols in {"runs": {"queued": "INTEGER", "queue_expired": "INTEGER", "sources": "TEXT",
                                     "triaged_out": "INTEGER", "stopped_by_time": "INTEGER",
                                     "search_seconds": "INTEGER", "triage_seconds": "INTEGER",
                                     "score_seconds": "INTEGER", "gemini_calls": "INTEGER",
                                     "gemini_errors": "INTEGER", "tokens_in": "INTEGER", "tokens_out": "INTEGER",
                                     "window_hours": "INTEGER"},
                            "triaged": {"reason": "TEXT"},
                            "offers": {"fingerprint": "TEXT", "synced": "INTEGER DEFAULT 0",
                                       **{c: "INTEGER" for c in ("score_requirements", "score_experience",
                                                                 "score_education", "score_languages",
                                                                 "score_extras")}}}.items():
            have = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.con.execute("CREATE INDEX IF NOT EXISTS offers_fp ON offers(fingerprint)")
        self.con.commit()

    # ------------------------------------------------------------ seen ids
    def seen_ids(self) -> set[str]:
        ids = {r[0] for r in self.con.execute("SELECT id FROM offers")}
        ids |= {r[0] for r in self.con.execute("SELECT id FROM seen")}
        ids |= {r[0] for r in self.con.execute("SELECT id FROM queue")}  # handled from the queue, not as new
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

    # ------------------------------------------------------------ queue
    def enqueue(self, offers: list[dict]) -> None:
        self.con.executemany(
            "INSERT OR IGNORE INTO queue(id, offer, queued_at) VALUES (?, ?, ?)",
            [(o["id"], json.dumps({k: o.get(k) for k in QUEUE_FIELDS}, ensure_ascii=False),
              now_iso()) for o in offers])
        self.con.commit()

    def queued(self) -> list[dict]:
        """Oldest first, so nothing waits forever."""
        return [json.loads(r[0]) for r in self.con.execute("SELECT offer FROM queue ORDER BY queued_at, id")]

    def dequeue(self, offer_id: str) -> None:
        self.con.execute("DELETE FROM queue WHERE id = ?", (offer_id,))
        self.con.commit()

    def queue_failed(self, offer: dict, max_attempts: int = 3) -> None:
        """Keep a failed offer for the next run; give up after max_attempts."""
        self.enqueue([offer])
        self.con.execute("UPDATE queue SET attempts = attempts + 1 WHERE id = ?", (offer["id"],))
        r = self.con.execute("SELECT attempts FROM queue WHERE id = ?", (offer["id"],)).fetchone()
        if r and r[0] >= max_attempts:
            self.dequeue(offer["id"])
            self.mark_seen(offer["id"])
        self.con.commit()

    def expire_queue(self, max_age_days: int) -> int:
        """Drop offers that waited too long (they are probably closed by now)."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).replace(microsecond=0).isoformat()
        old = [r[0] for r in self.con.execute("SELECT id FROM queue WHERE queued_at < ?", (cutoff,))]
        for i in old:
            self.dequeue(i)
            self.mark_seen(i)
        return len(old)

    def recent_fingerprints(self, days: int = 30) -> set[str]:
        """company|title of offers scored recently or waiting in the queue - for cross-portal de-duplication."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        fps = {r[0] for r in self.con.execute(
            "SELECT fingerprint FROM offers WHERE fingerprint IS NOT NULL AND first_seen >= ?", (cutoff,))}
        fps |= {o.get("fingerprint") for o in self.queued() if o.get("fingerprint")}
        return fps

    def save_triaged(self, offers: list[dict]) -> None:
        self.con.executemany(
            "INSERT OR IGNORE INTO triaged(id, source, title, company, location, url, triaged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(o["id"], o.get("source"), o.get("title"), o.get("company"), o.get("location"), o.get("url"), now_iso())
             for o in offers])
        self.con.executemany("INSERT OR IGNORE INTO seen(id) VALUES (?)", [(o["id"],) for o in offers])
        self.con.commit()

    def save_skipped(self, offers: list[dict], reason: str) -> None:
        """Offers dropped by the title filter or as cross-portal duplicates - stored only so "Find offer" can
        explain them. NOT marked as seen: after a config change they are considered again."""
        self.con.executemany(
            "INSERT OR IGNORE INTO triaged(id, source, title, company, location, url, triaged_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(o["id"], o.get("source"), o.get("title"), o.get("company"), o.get("location"), o.get("url"), now_iso(),
              reason) for o in offers])
        self.con.commit()

    def triaged_since(self, since_iso: str) -> list[dict]:
        """Offers the AI pre-filter dropped (not the title-filter / duplicate records)."""
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM triaged WHERE triaged_at >= ? AND (reason IS NULL OR reason = 'ai') ORDER BY triaged_at",
            (since_iso,))]

    def triaged_csv(self, since_iso: str) -> str:
        buf = io.StringIO()
        buf.write("\ufeff")
        w = csv.writer(buf)
        w.writerow(["triaged_at", "source", "title", "company", "location", "url"])
        for t in self.triaged_since(since_iso):
            w.writerow([t["triaged_at"], t["source"], t["title"], t["company"], t["location"], t["url"]])
        return buf.getvalue()

    def find(self, query: str) -> dict:
        """Where did an offer end up? query = a link from any portal, or words from its title / company."""
        from sources.common import normalise
        q = query.strip()
        ids = set(re.findall(r"oferta,(\d+)", q)) | set(re.findall(r"/jobs/view/(?:[^/]*?-)?(\d+)", q)) | \
            set(re.findall(r"currentJobId=(\d+)", q))
        slug = re.findall(r"/(?:job-offer|oferta-pracy|job)/([a-z0-9-]+)", q)
        words = normalise(q).split() if not (ids or slug) else []

        def hit(url, title, company):
            u = url or ""
            if ids and any(i in u for i in ids):
                return True
            if slug and any(sl in u for sl in slug):
                return True
            text = normalise(f"{title} {company}")
            return bool(words) and all(w in text for w in words)

        scored = [dict(r) for r in self.con.execute(
            "SELECT id, source, title, company, location, url, score, verdict, gaps, one_line_reason, first_seen, "
            "notified FROM offers") if hit(r["url"], r["title"], r["company"])]
        skipped = [dict(r) for r in self.con.execute("SELECT * FROM triaged")
                   if hit(r["url"], r["title"], r["company"])]
        queued = [o for o in self.queued() if hit(o.get("url"), o.get("title"), o.get("company"))]
        return {"scored": scored, "skipped": skipped, "queued": queued}

    def rescue(self, offers: list[dict]) -> int:
        """Queue skipped offers the user marked "interested" (each only once, and not if already scored)."""
        done = {r[0] for r in self.con.execute("SELECT id FROM rescued")}
        done |= {r[0] for r in self.con.execute("SELECT id FROM offers")}
        done |= {r[0] for r in self.con.execute("SELECT id FROM queue")}
        new = [o for o in offers if o.get("id") and o["id"] not in done]
        self.enqueue([{**o, "search_keyword": o.get("search_keyword") or "marked interested"} for o in new])
        self.con.executemany("DELETE FROM seen WHERE id = ?", [(o["id"],) for o in new])
        self.con.executemany("INSERT OR IGNORE INTO rescued(id, rescued_at) VALUES (?, ?)",
                             [(o["id"], now_iso()) for o in new])
        self.con.commit()
        return len(new)

    def queue_size(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    # ------------------------------------------------------------ offers
    def save_offer(self, row: dict, description: str = "") -> None:
        data = {k: row.get(k) for k in EXPORT_COLUMNS + ["fingerprint"] if k in row}
        data["synced"] = 0  # changed -> send to Supabase on the next sync
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
        keep = {"id", "first_seen", "user_label", "status", "notified"}  # synced is reset by the update
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in keep)
        self.con.execute(
            f"INSERT INTO offers ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
            list(data.values()))
        self.con.commit()

    def mark_notified(self, ids: list[str]) -> None:
        self.con.executemany("UPDATE offers SET notified = 1, synced = 0 WHERE id = ?", [(i,) for i in ids])
        self.con.commit()

    def description(self, offer_id: str) -> str:
        r = self.con.execute("SELECT description_z FROM offers WHERE id = ?", (offer_id,)).fetchone()
        return zlib.decompress(r[0]).decode("utf-8") if r and r[0] else ""

    # ------------------------------------------------------------ runs
    def ran_today(self) -> bool:
        """True if a run finished today (UTC) - used to skip the backup schedule."""
        today = datetime.now(timezone.utc).date().isoformat()
        return bool(self.con.execute(
            "SELECT 1 FROM runs WHERE finished_at >= ? LIMIT 1", (today,)).fetchone())

    def last_full_scan_start(self, min_window: int = 20) -> datetime | None:
        """Start of the latest scan that looked back at least min_window hours (short manual test runs,
        e.g. 1 h to empty the queue, don't count as coverage)."""
        r = self.con.execute(
            "SELECT MAX(started_at) FROM runs WHERE finished_at IS NOT NULL AND "
            "(window_hours IS NULL OR window_hours >= ?)", (min_window,)).fetchone()
        try:
            return datetime.fromisoformat(r[0]) if r and r[0] else None
        except ValueError:
            return None

    def report_sent_today(self) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        return bool(self.con.execute("SELECT 1 FROM reports WHERE sent_at >= ? LIMIT 1", (today,)).fetchone())

    def mark_report_sent(self) -> None:
        self.con.execute("INSERT INTO reports(sent_at) VALUES (?)", (now_iso(),))
        self.con.commit()

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


# ---------------------------------------------------------------- encryption at rest
# The database is kept in the GitHub Actions cache. The repo is public, so it is stored there only
# encrypted (Fernet = AES-128-CBC + HMAC-SHA256) with a key from the DB_ENCRYPTION_KEY secret.
def _fernet(secret: str):
    import base64
    import hashlib

    from cryptography.fernet import Fernet
    try:  # a real Fernet key (from Fernet.generate_key())
        return Fernet(secret.encode())
    except ValueError:  # any long passphrase works too - derive a key from it
        key = hashlib.scrypt(secret.encode(), salt=b"job-scanner-db", n=2 ** 14, r=8, p=1, dklen=32)
        return Fernet(base64.urlsafe_b64encode(key))


def encrypt_file(src: Path, dst: Path, secret: str) -> None:
    dst.write_bytes(_fernet(secret).encrypt(src.read_bytes()))


def decrypt_file(src: Path, dst: Path, secret: str) -> None:
    from cryptography.fernet import InvalidToken
    try:
        dst.write_bytes(_fernet(secret).decrypt(src.read_bytes()))
    except InvalidToken:
        raise SystemExit("Could not decrypt the database - DB_ENCRYPTION_KEY is different from the one "
                         "used to encrypt it. Restore the old key (or delete the Actions cache to start over).")
