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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tidy_offers(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    for c in JSON_COLUMNS:
        if c in df:
            df[c] = df[c].apply(lambda v: v if isinstance(v, list) else (json.loads(v) if v else []))
    df["first_seen"] = pd.to_datetime(df["first_seen"], utc=True, errors="coerce")
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
        return pd.DataFrame(rows)

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
        df = self._all("applications")
        return df if not df.empty else pd.DataFrame(columns=APP_COLUMNS)

    def save_application(self, offer_id: str, **fields) -> None:
        row = {"offer_id": offer_id, **{k: v for k, v in fields.items() if k in APP_COLUMNS}, "updated_at": _now()}
        self.c.table("applications").upsert(row).execute()

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
        """)

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
        return pd.DataFrame(rows)

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
