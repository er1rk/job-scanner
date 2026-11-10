"""Data access for the web app.

SupabaseBackend - the live app: reads the scanner's tables and writes your applications / settings,
                  as the logged-in user (Row Level Security applies).
LocalBackend    - development and screenshots: the scanner's SQLite file (data/jobs.db) plus two
                  local tables for applications and settings.
Both return pandas DataFrames with the same columns, so the UI doesn't know which one it talks to.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

STATUSES = ["saved", "applied", "interview", "offer", "rejected", "not interested"]
SETTING_KEYS = ["keywords", "locations", "include_title_keywords", "exclude_title_keywords", "min_score_to_notify"]
JSON_COLUMNS = ["strengths", "gaps", "contract_types", "required_tools", "nice_to_have_tools"]
APP_COLUMNS = ["offer_id", "status", "applied_on", "follow_up_on", "notes", "updated_at"]
# columns each table has - used so an empty table (fresh Supabase, before the first sync) still has them
COLUMNS = {
    "offers": ["id", "source", "first_seen", "title", "company", "location", "url", "search_keyword", "score",
               "verdict", "seniority", "one_line_reason", "strengths", "gaps", "notified", "role_category", "industry",
               "work_mode", "contract_types", "salary_min", "salary_max", "salary_currency", "salary_period",
               "salary_type", "salary_min_pln_month", "salary_max_pln_month", "years_required", "english_level",
               "polish_required", "required_tools", "nice_to_have_tools", "model"],
    "runs": ["run_id", "started_at", "finished_at", "window_hours", "cards_found", "filtered_title", "new_offers",
             "triaged_out", "scored", "failed", "notified", "queued", "stopped_by_time", "gemini_calls",
             "gemini_errors", "sources", "model"],
    "queue": ["id", "source", "title", "company", "location", "url", "queued_at"],
    "skipped": ["id", "source", "title", "company", "location", "url", "reason", "skipped_at"],
    "applications": APP_COLUMNS,
    "feedback": ["id", "source", "title", "company", "location", "url", "reason", "marked_at"],
}
FEEDBACK_COLUMNS = ["id", "source", "title", "company", "location", "url", "reason"]


def _frame(rows: list[dict], table: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in COLUMNS.get(table, []):
        if c not in df:
            df[c] = pd.Series(dtype="object")
    if table in ("queue", "skipped", "applications", "feedback"):
        df = _text_none(df, [c for c in df.columns if c not in ("applied_on", "follow_up_on")])
    return df


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


TEXT_COLUMNS = ["source", "title", "company", "location", "url", "search_keyword", "verdict", "seniority",
                "one_line_reason", "role_category", "industry", "work_mode", "salary_currency", "salary_period",
                "salary_type", "english_level", "model"]


def _text_none(df: pd.DataFrame, cols) -> pd.DataFrame:
    """pandas 3 keeps missing text as NaN (a float, and truthy) - turn it back into None so `x or default` works."""
    for c in cols:
        if c in df:
            df[c] = df[c].astype(object).where(df[c].notna(), None)
    return df


def _tidy_offers(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = _text_none(df, TEXT_COLUMNS)
    for c in JSON_COLUMNS:
        if c in df:
            df[c] = df[c].apply(lambda v: v if isinstance(v, list) else (json.loads(v) if v else []))
    df["first_seen"] = pd.to_datetime(df["first_seen"], utc=True, errors="coerce")
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df["notified"] = df["notified"].fillna(False).astype(bool)
    return df


class SupabaseBackend:
    PAGE = 1000  # PostgREST returns at most this many rows per request

    def __init__(self, client):
        self.c = client

    def _all(self, table: str, order: str | None = None) -> pd.DataFrame:
        rows, start = [], 0
        while True:
            q = self.c.table(table).select("*")
            if order:
                q = q.order(order, desc=True)
            batch = q.range(start, start + self.PAGE - 1).execute().data
            rows += batch
            if len(batch) < self.PAGE:
                break
            start += self.PAGE
        return _frame(rows, table)

    def offers(self):
        return _tidy_offers(self._all("offers", "first_seen"))

    def runs(self):
        df = self._all("runs", "started_at")
        for c in ("started_at", "finished_at"):
            if c in df:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        return df

    def queue(self):
        return self._all("queue")

    def skipped(self):
        return self._all("skipped", "skipped_at")

    def applications(self):
        return self._all("applications")

    def save_application(self, offer_id: str, **fields) -> None:
        row = {"offer_id": offer_id, **{k: v for k, v in fields.items() if k in APP_COLUMNS}, "updated_at": _now()}
        self.c.table("applications").upsert(row).execute()

    def feedback(self):
        return self._all("feedback", "marked_at")

    def cv(self, offer_id: str) -> dict | None:
        rows = self.c.table("cvs").select("*").eq("offer_id", offer_id).execute().data
        return rows[0] if rows else None

    def mark_interested(self, rows: list[dict]) -> None:
        self.c.table("feedback").upsert([{k: r.get(k) for k in FEEDBACK_COLUMNS} | {"marked_at": _now()}
                                         for r in rows]).execute()

    def unmark_interested(self, ids: list[str]) -> None:
        self.c.table("feedback").delete().in_("id", ids).execute()

    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.c.table("settings").select("*").execute().data}

    def effective_settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.c.table("effective_settings").select("*").execute().data}

    def save_setting(self, key: str, value) -> None:
        if value in (None, [], ""):
            self.c.table("settings").delete().eq("key", key).execute()
        else:
            self.c.table("settings").upsert({"key": key, "value": value, "updated_at": _now()}).execute()


class LocalBackend:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS app_applications (offer_id TEXT PRIMARY KEY, status TEXT, applied_on TEXT,
                follow_up_on TEXT, notes TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS app_feedback (id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
                location TEXT, url TEXT, reason TEXT, marked_at TEXT);
        """)

    def feedback(self):
        return _frame([dict(r) for r in self.con.execute("SELECT * FROM app_feedback ORDER BY marked_at DESC")],
                      "feedback")

    def cv(self, offer_id: str) -> dict | None:  # CVs live only in Supabase
        return None

    def mark_interested(self, rows: list[dict]) -> None:
        self.con.executemany("INSERT OR REPLACE INTO app_feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             [tuple(r.get(k) for k in FEEDBACK_COLUMNS) + (_now(),) for r in rows])
        self.con.commit()

    def unmark_interested(self, ids: list[str]) -> None:
        self.con.executemany("DELETE FROM app_feedback WHERE id = ?", [(i,) for i in ids])
        self.con.commit()

    def _df(self, sql: str) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.con)

    def offers(self):
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(offers)") if r[1] not in ("description_z",)]
        return _tidy_offers(self._df(f"SELECT {', '.join(cols)} FROM offers ORDER BY first_seen DESC"))

    def runs(self):
        df = self._df("SELECT * FROM runs ORDER BY started_at DESC")
        if "sources" in df:
            df["sources"] = df["sources"].apply(lambda v: json.loads(v) if v else None)
        for c in ("started_at", "finished_at"):
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        return df

    def queue(self):
        rows = [json.loads(r[0]) | {"queued_at": r[1]} for r in self.con.execute("SELECT offer, queued_at FROM queue")]
        return _frame(rows, "queue")

    def skipped(self):
        df = self._df("SELECT id, source, title, company, location, url, COALESCE(reason, 'ai') AS reason, "
                      "triaged_at AS skipped_at FROM triaged ORDER BY triaged_at DESC")
        return df

    def applications(self):
        return self._df(f"SELECT {', '.join(APP_COLUMNS)} FROM app_applications")

    def save_application(self, offer_id: str, **fields) -> None:
        cur = dict(self.con.execute("SELECT * FROM app_applications WHERE offer_id = ?", (offer_id,)).fetchone() or {})
        row = {**{c: None for c in APP_COLUMNS}, **cur, **{k: v for k, v in fields.items() if k in APP_COLUMNS},
               "offer_id": offer_id, "updated_at": _now()}
        row = {k: (str(v) if v is not None and k in ("applied_on", "follow_up_on") else v) for k, v in row.items()}
        self.con.execute(f"INSERT OR REPLACE INTO app_applications ({', '.join(APP_COLUMNS)}) VALUES "
                         f"({', '.join('?' for _ in APP_COLUMNS)})", [row[c] for c in APP_COLUMNS])
        self.con.commit()

    def settings(self) -> dict:
        return {r[0]: json.loads(r[1]) for r in self.con.execute("SELECT key, value FROM app_settings")}

    def effective_settings(self) -> dict:
        """Locally: the public defaults from config.yaml (+ config.private.yaml if present)."""
        import yaml
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
        if (root / "config.private.yaml").exists():
            cfg.update(yaml.safe_load((root / "config.private.yaml").read_text(encoding="utf-8")) or {})
        return {k: cfg[k] for k in SETTING_KEYS if k in cfg}

    def save_setting(self, key: str, value) -> None:
        if value in (None, [], ""):
            self.con.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            self.con.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?, ?)", (key, json.dumps(value), _now()))
        self.con.commit()
