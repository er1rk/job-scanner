"""
Push the scanner's results to Supabase (Postgres) for the web app, and read the search settings
the app lets you edit.

The scanner keeps working on its own SQLite database (encrypted in the GitHub Actions cache);
Supabase is a copy the app reads from. Sync is one-way for scanner data (offers, runs, queue,
skipped) - the app owns `applications` and `settings`, so the two never overwrite each other.

Talks to Supabase's REST API (PostgREST) with plain `requests` and the service_role key, which
bypasses Row Level Security - it lives only in GitHub Secrets. A failing sync never fails a scan.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import requests

BATCH = 500
SETTING_KEYS = {"keywords": list, "locations": list, "include_title_keywords": list,
                "exclude_title_keywords": list, "min_score_to_notify": int}
OFFER_COLUMNS = [
    "id", "source", "first_seen", "title", "company", "location", "url", "search_keyword", "score", "verdict",
    "seniority", "one_line_reason", "strengths", "gaps", "notified", "role_category", "industry", "work_mode",
    "contract_types", "salary_min", "salary_max", "salary_currency", "salary_period", "salary_type",
    "salary_min_pln_month", "salary_max_pln_month", "years_required", "english_level", "polish_required",
    "required_tools", "nice_to_have_tools", "model",
]
JSON_COLUMNS = {"strengths", "gaps", "contract_types", "required_tools", "nice_to_have_tools"}
RUN_COLUMNS = [
    "run_id", "started_at", "finished_at", "window_hours", "cards_found", "filtered_title", "new_offers",
    "triaged_out", "scored", "failed", "notified", "queued", "stopped_by_time", "search_seconds",
    "triage_seconds", "score_seconds", "gemini_calls", "gemini_errors", "tokens_in", "tokens_out", "sources", "model",
]


def normalise_url(url: str) -> str:
    """Forgive the usual copy-paste slips in the SUPABASE_URL secret: quotes, spaces, a missing https://,
    a trailing /rest/v1, or just the project id ("abcd1234" -> https://abcd1234.supabase.co)."""
    u = (url or "").strip().strip("'\"").strip().rstrip("/")
    u = re.sub(r"/rest/v1$", "", u)
    if re.fullmatch(r"[a-z0-9]{15,30}", u):
        u = f"{u}.supabase.co"
    if not re.match(r"https?://", u):
        u = "https://" + u
    return u


class Supabase:
    def __init__(self, url: str, key: str):
        self.base = normalise_url(url) + "/rest/v1"
        key = (key or "").strip().strip("'\"").strip()
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        r = requests.request(method, f"{self.base}/{path}", headers={**self.headers, **kw.pop("headers", {})},
                             timeout=60, **kw)
        if r.status_code >= 300:
            # PostgREST errors describe the table/column, never row contents - safe for public logs
            raise RuntimeError(f"{method} {path.split('?')[0]}: {r.status_code} {r.text[:200]}")
        return r

    def upsert(self, table: str, rows: list[dict], key: str = "id") -> None:
        for i in range(0, len(rows), BATCH):
            self._call("POST", f"{table}?on_conflict={key}", json=rows[i:i + BATCH],
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})

    def delete(self, table: str, filter_: str) -> None:
        self._call("DELETE", f"{table}?{filter_}", headers={"Prefer": "return=minimal"})

    def select(self, table: str, query: str = "select=*") -> list[dict]:
        return self._call("GET", f"{table}?{query}").json()


def _offer_row(r) -> dict:
    row = {c: r[c] for c in OFFER_COLUMNS}
    for c in JSON_COLUMNS:
        row[c] = json.loads(row[c]) if row[c] else []
    row["notified"] = bool(row["notified"])
    row["polish_required"] = None if row["polish_required"] is None else bool(row["polish_required"])
    return row


def sync(store, url: str, key: str, cfg: dict | None = None) -> dict:
    """Send what changed since the last sync. Returns counts (safe to log)."""
    sb = Supabase(url, key)
    con = store.con
    pending = con.execute(f"SELECT {', '.join(OFFER_COLUMNS)} FROM offers WHERE synced = 0").fetchall()
    sb.upsert("offers", [_offer_row(r) for r in pending])
    con.executemany("UPDATE offers SET synced = 1 WHERE id = ?", [(r["id"],) for r in pending])
    con.commit()

    have = {c[1] for c in con.execute("PRAGMA table_info(runs)")}
    cols = [c for c in RUN_COLUMNS if c in have]
    runs = []
    for r in con.execute(f"SELECT {', '.join(cols)} FROM runs ORDER BY run_id DESC LIMIT 60"):
        row = dict(zip(cols, r))
        row["sources"] = json.loads(row["sources"]) if row.get("sources") else None
        runs.append(row)
    sb.upsert("runs", runs, key="run_id")

    queue = [{k: o.get(k) for k in ("id", "source", "title", "company", "location", "url")} for o in store.queued()]
    queued_at = {r[0]: r[1] for r in con.execute("SELECT id, queued_at FROM queue")}
    for q in queue:
        q["queued_at"] = queued_at.get(q["id"])
    sb.delete("queue", "id=not.is.null")  # the queue is a snapshot - replace it
    sb.upsert("queue", queue)

    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    skipped = [{"id": r["id"], "source": r["source"], "title": r["title"], "company": r["company"],
                "location": r["location"], "url": r["url"], "reason": r["reason"] or "ai",
                "skipped_at": r["triaged_at"]}
               for r in con.execute("SELECT * FROM triaged WHERE triaged_at >= ?", (since,))]
    sb.upsert("skipped", skipped)
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    sb.delete("skipped", f"skipped_at=lt.{month_ago}")
    if cfg:  # what this run actually used, so the app can show (and pre-fill) the current settings
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        sb.upsert("effective_settings", [{"key": k, "value": cfg[k], "updated_at": now}
                                         for k in SETTING_KEYS if k in cfg], key="key")
    return {"offers": len(pending), "runs": len(runs), "queue": len(queue), "skipped": len(skipped)}


def load_settings(url: str, key: str) -> dict:
    """Search settings saved in the web app - only known keys with the right type are used."""
    out = {}
    for row in Supabase(url, key).select("settings", "select=key,value"):
        k, v = row.get("key"), row.get("value")
        want = SETTING_KEYS.get(k)
        if want is list and isinstance(v, list) and all(isinstance(x, str) for x in v) and v:
            out[k] = [x.strip() for x in v if x.strip()]
        elif want is int and isinstance(v, (int, float)) and 0 <= v <= 100:
            out[k] = int(v)
    return out
