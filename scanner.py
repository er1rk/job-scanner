"""
AI Job Scanner
--------------
1. Pulls fresh job offers from several portals (LinkedIn, pracuj.pl, No Fluff Jobs, justjoin.it - see sources/).
2. Filters out obvious mismatches by title and drops the same job seen on another portal.
3. Asks Google Gemini to score each offer against the candidate profile
   using a recruiter-style rubric (0-100) and to extract structured facts
   (salary, work mode, contract, required tools, English level...).
4. Saves every scored offer to SQLite (data/jobs.db) and e-mails the best matches.

Run locally:   python scanner.py --dry-run      (prints results, sends nothing, saves nothing)
Run for real:  python scanner.py                (needs env vars, see README)
Export data:   python scanner.py --export       (writes data/jobs.csv for Power BI / Excel)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

import report
import sources
from sources import common
from storage import Store, decrypt_file, encrypt_file, now_iso

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "data" / "seen.json"   # legacy cache, migrated into the database on first run
DB_FILE = ROOT / "data" / "jobs.db"
DB_ENC_FILE = ROOT / "data" / "jobs.db.enc"   # the only form that is cached in GitHub Actions
CSV_FILE = ROOT / "data" / "jobs.csv"
REPORT_FILE = ROOT / "data" / "report.html"
# ---------------------------------------------------------------- helpers
def load_config() -> dict:
    """Public defaults from config.yaml, overridden by a private config that never lands in the repo:
    the PRIVATE_CONFIG secret (YAML text) in GitHub Actions, or config.private.yaml locally (git-ignored).
    Keys are replaced as a whole, e.g. a private `keywords` list replaces the public one."""
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    private = os.getenv("PRIVATE_CONFIG")
    if not private and (ROOT / "config.private.yaml").exists():
        private = (ROOT / "config.private.yaml").read_text(encoding="utf-8")
    if private:
        override = yaml.safe_load(private)
        if not isinstance(override, dict):
            sys.exit("PRIVATE_CONFIG must be YAML key: value pairs")
        cfg.update(override)
        print(f"Private config applied ({len(override)} keys)")  # key count only - logs are public
    return cfg


def load_profile() -> str:
    """Real profile comes from a secret, so it never lands in a public repo."""
    profile = os.getenv("CANDIDATE_PROFILE")
    if profile:
        return profile
    for name in ("profile.md", "profile.example.md"):
        p = ROOT / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    sys.exit("No candidate profile found (set CANDIDATE_PROFILE or create profile.md)")


def build_searches(cfg: dict) -> list[dict]:
    """Every keyword x every location, plus remote-in-Poland, plus any explicit 'searches'."""
    searches = list(cfg.get("searches") or [])
    for kw in cfg.get("keywords", []):
        for loc in cfg.get("locations", []):
            searches.append({"keywords": kw, "location": loc})
        if cfg.get("include_remote_poland"):
            searches.append({"keywords": kw, "location": "Poland", "remote": True})
    return searches


def _title_ok(title: str, cfg: dict) -> bool:
    t = common.strip_accents(title.lower())
    if any(common.strip_accents(w.lower()) in t for w in cfg.get("exclude_title_keywords") or []):
        return False
    include = cfg.get("include_title_keywords") or []
    return not include or any(common.strip_accents(w.lower()) in t for w in include)


def round_robin(groups: list[list[dict]]) -> list[dict]:
    """Interleave sources so the per-run cap is shared fairly instead of filled by the first portal."""
    out, i = [], 0
    while any(i < len(g) for g in groups):
        out += [g[i] for g in groups if i < len(g)]
        i += 1
    return out


def fetch_offers(cfg: dict, seen: set, seen_fps: set, stats: dict) -> list[dict]:
    hours = int(cfg["posted_within_hours"])
    searches = build_searches(cfg)
    run_fps: set[str] = set()
    groups, per_source, failed_sources = [], {}, []
    for name in cfg.get("sources") or ["linkedin"]:
        mod = sources.ALL.get(name)
        if not mod:
            print(f"Unknown source '{name}' - skipped")
            continue
        try:
            found = mod.search(searches, cfg, hours)
        except Exception as e:  # one broken portal must not stop the others
            print(f"{name}: search failed ({type(e).__name__})")
            failed_sources.append(name)
            found = []
        st = {"cards": len(found), "filtered": 0, "duplicates": 0, "new": 0}
        new, ids = [], set()
        for o in found:
            o.setdefault("source", name)
            o["fingerprint"] = common.fingerprint(o.get("company"), o.get("title"))
            if o["id"] in seen or o["id"] in ids:
                continue  # already known (or listed twice by this portal)
            ids.add(o["id"])
            if not _title_ok(o.get("title", ""), cfg):
                st["filtered"] += 1
                continue
            if o["fingerprint"] in seen_fps or o["fingerprint"] in run_fps:
                st["duplicates"] += 1  # same company + title already found on another portal
                continue
            run_fps.add(o["fingerprint"])
            new.append(o)
        st["new"] = len(new)
        per_source[name] = st
        groups.append(new)
        print(f"  {name}: cards {st['cards']}, dropped by title {st['filtered']}, "
              f"cross-portal duplicates {st['duplicates']}, new {st['new']}")
    stats.update(searches=len(searches), cards_found=sum(v["cards"] for v in per_source.values()),
                 filtered_title=sum(v["filtered"] for v in per_source.values()),
                 new_offers=sum(v["new"] for v in per_source.values()),
                 sources=json.dumps(per_source), failed_sources=failed_sources)
    print(f"Searches: {len(searches)} per source, unique new offers: {stats['new_offers']}")
    return round_robin(groups)  # the per-run cap is applied in main(), the rest goes to the queue


# ---------------------------------------------------------------- Gemini
RUBRIC = """You are a strict technical recruiter. Score how well the candidate fits the job offer, 0-100:
- mandatory requirements: max 40
- years and type of experience: max 20
- education: max 15
- languages: max 10
- nice-to-haves and industry: max 15
Subtract points for excluding conditions (e.g. required degree/certificate the candidate lacks,
required language they don't speak, far more years required). Do NOT penalise work mode (remote/hybrid/office).
Never assume skills the profile does not state. Answer in English.

Write each gap as a short, reusable skill/requirement name (e.g. "SQL", "SAP", "German C1",
"3+ years experience") so gaps can be counted across many offers.

Also extract facts from the offer itself (independent of the candidate). Use null when the offer
does not state something - never guess salaries. Salary: copy the numbers as written, with their
currency, period (hour/day/month/year) and whether they are gross, net or B2B net ("unknown" if unclear).
required_tools / nice_to_have_tools: concrete tools and technologies only (Excel, Power BI, SQL, Python, SAP...)."""

DEFAULT_ROLE_CATEGORIES = ["data/analytics", "finance", "operations", "engineering",
                           "sales/marketing", "other"]


def build_schema(cfg: dict) -> dict:
    """Role categories come from the (private) config, so the public code doesn't reveal target roles."""
    cats = [str(c) for c in (cfg.get("role_categories") or DEFAULT_ROLE_CATEGORIES)]
    if "other" not in cats:
        cats.append("other")
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["role_category"]["enum"] = cats
    return schema


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        # --- fit
        "score": {"type": "INTEGER"},
        "verdict": {"type": "STRING", "enum": ["apply", "stretch", "skip"]},
        "seniority": {"type": "STRING", "enum": ["intern", "junior", "mid", "senior", "lead", "unknown"]},
        "strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
        "gaps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "one_line_reason": {"type": "STRING"},
        # --- facts about the offer
        "role_category": {"type": "STRING", "enum": DEFAULT_ROLE_CATEGORIES},  # replaced by build_schema
        "industry": {"type": "STRING", "nullable": True},
        "work_mode": {"type": "STRING", "enum": ["remote", "hybrid", "on-site", "unknown"]},
        "contract_types": {"type": "ARRAY", "items": {"type": "STRING", "enum": [
            "employment contract", "B2B", "mandate contract", "other"]}},
        "salary_min": {"type": "NUMBER", "nullable": True},
        "salary_max": {"type": "NUMBER", "nullable": True},
        "salary_currency": {"type": "STRING", "nullable": True},
        "salary_period": {"type": "STRING", "nullable": True, "enum": ["hour", "day", "month", "year"]},
        "salary_type": {"type": "STRING", "nullable": True, "enum": ["gross", "net", "unknown"]},
        "years_required": {"type": "NUMBER", "nullable": True},
        "english_level": {"type": "STRING", "enum": ["none", "A2", "B1", "B2", "C1", "C2", "unknown"]},
        "polish_required": {"type": "BOOLEAN", "nullable": True},
        "required_tools": {"type": "ARRAY", "items": {"type": "STRING"}},
        "nice_to_have_tools": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["score", "verdict", "strengths", "gaps", "one_line_reason",
                 "role_category", "work_mode", "english_level", "required_tools"],
}

PERIOD_TO_MONTH = {"hour": 168, "day": 21, "month": 1, "year": 1 / 12}


def normalise_salary(s: dict, cfg: dict) -> dict:
    """Convert the extracted range to PLN per month so offers can be compared.
    Gross vs. B2B net is NOT converted - salary_type stays in the data for filtering."""
    rates = {k.upper(): v for k, v in (cfg.get("fx_to_pln") or {"PLN": 1}).items()}
    cur = (s.get("salary_currency") or "").upper().strip()
    cur = {"ZŁ": "PLN", "ZL": "PLN", "€": "EUR", "$": "USD", "£": "GBP"}.get(cur, cur)
    factor = PERIOD_TO_MONTH.get(s.get("salary_period") or "month")
    out = {"salary_min_pln_month": None, "salary_max_pln_month": None}
    if cur in rates and factor:
        for k in ("min", "max"):
            v = s.get(f"salary_{k}")
            if isinstance(v, (int, float)) and v > 0:
                out[f"salary_{k}_pln_month"] = round(v * factor * rates[cur])
    return out


def salary_label(r: dict) -> str:
    lo, hi = r.get("salary_min"), r.get("salary_max")
    if not (lo or hi):
        return ""
    rng = f"{lo:,.0f}–{hi:,.0f}" if lo and hi and lo != hi else f"{(lo or hi):,.0f}"
    parts = [rng.replace(",", " "), r.get("salary_currency") or "", "/ " + (r.get("salary_period") or "month")]
    if r.get("salary_type") and r["salary_type"] != "unknown":
        parts.append(r["salary_type"])
    return " ".join(p for p in parts if p)


def pick_available_model(api_key: str, exclude: str = "") -> str | None:
    """If the configured model doesn't exist for this key, find a Flash model that does."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": api_key, "pageSize": 200}, timeout=30)
    if r.status_code != 200:
        print(f"  Could not list models: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return None
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if "flash" in n and n != exclude
             and not any(x in n for x in ("tts", "image", "live", "embedding"))]
    # prefer stable over preview, newest first
    flash.sort(key=lambda n: ("preview" in n or "exp" in n, [-int(x) for x in re.findall(r"\d+", n)[:2]]))
    print(f"  Available Flash models: {', '.join(flash[:6]) or 'none'}")
    return flash[0] if flash else None


# Gemini usage of the current run, written to the runs table (monitoring of cost and reliability)
USAGE = {"gemini_calls": 0, "gemini_errors": 0, "tokens_in": 0, "tokens_out": 0}


def _gemini_failed() -> None:
    USAGE["gemini_errors"] += 1
    return None


def call_gemini(prompt: str, schema: dict, cfg: dict, api_key: str, temperature: float = 0.2) -> dict | None:
    """One structured-output call with retries, rate-limit back-off and model fallback.
    Returns the parsed JSON object, or None if every attempt failed."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    for attempt in range(5):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}:generateContent"
        try:
            r = requests.post(url, params={"key": api_key}, json=body, timeout=120)
        except requests.RequestException as e:  # timeout / connection drop - retry, never crash the run
            print(f"  Gemini request failed ({type(e).__name__}), retrying", file=sys.stderr)
            time.sleep(10)
            continue
        if r.status_code == 429:  # free-tier rate limit
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code in (500, 503):  # model overloaded - wait, then switch to another model
            if attempt >= 1 and not cfg.get("_fallback_used"):
                cfg["_fallback_used"] = True
                m = pick_available_model(api_key, exclude=cfg["gemini_model"])
                if m:
                    print(f"  '{cfg['gemini_model']}' overloaded - switching to {m}")
                    cfg["gemini_model"] = m
                    continue
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code == 404 and not cfg.get("_model_checked"):
            cfg["_model_checked"] = True
            print(f"  Model '{cfg['gemini_model']}' not available - looking for another one")
            m = pick_available_model(api_key)
            if m:
                cfg["gemini_model"] = m
                print(f"  Using model: {m}")
                continue
        if r.status_code != 200:
            print(f"  Gemini error {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return _gemini_failed()
        try:
            data = r.json()
            usage = data.get("usageMetadata") or {}
            USAGE["gemini_calls"] += 1
            USAGE["tokens_in"] += int(usage.get("promptTokenCount") or 0)
            USAGE["tokens_out"] += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
            return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError, TypeError):
            # empty, blocked or cut-off answer - log why (no content, logs are public) and try again
            try:
                reason = (data.get("candidates") or [{}])[0].get("finishReason") or \
                    (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
            except Exception:
                reason = "unreadable response"
            print(f"  Gemini answer unusable ({reason}), retrying", file=sys.stderr)
            time.sleep(5)
            continue
    return _gemini_failed()


TRIAGE_PROMPT = """You pre-screen job offers for the candidate below before a detailed review.
For every numbered offer you only see title, company, location and portal. Decide which are worth a detailed review.
Drop ONLY offers that are clearly not a fit, for example:
- a different profession (software developer/engineer, sales representative, accountant, recruiter, designer,
  nurse, driver...) that does not match the candidate's target roles;
- seniority clearly above the candidate (senior, lead, head of, manager, director, principal, expert);
- a language requirement in the title the candidate does not speak.
When unsure, KEEP the offer - a detailed review follows. Return the numbers of the offers to keep."""

TRIAGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"keep": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
    "required": ["keep"],
}


def triage_offers(offers: list[dict], profile: str, cfg: dict, api_key: str) -> tuple[list[dict], list[dict]]:
    """One cheap Gemini call per batch of titles drops clear mismatches before the per-offer scoring.
    Fails open: if a batch can't be triaged, all its offers are kept."""
    if not cfg.get("triage", True) or not offers:
        return offers, []
    size = int(cfg.get("triage_batch_size", 60))
    kept, dropped = [], []
    for start in range(0, len(offers), size):
        batch = offers[start:start + size]
        lines = "\n".join(f"{i}. {o.get('title', '')} | {o.get('company', '')} | {o.get('location', '')} | "
                          f"{sources.label(o.get('source'))}" for i, o in enumerate(batch, 1))
        res = call_gemini(f"{TRIAGE_PROMPT}\n\n### Candidate profile\n{profile}\n\n### Offers\n{lines}",
                          TRIAGE_SCHEMA, cfg, api_key, temperature=0.0)
        keep = {i for i in (res or {}).get("keep", []) if isinstance(i, int) and 1 <= i <= len(batch)}
        if res is None or "keep" not in res or (not keep and len(batch) >= 5):
            # failed call, malformed answer or a suspicious "drop everything" -> keep the whole batch
            kept += batch
        else:
            kept += [o for i, o in enumerate(batch, 1) if i in keep]
            dropped += [o for i, o in enumerate(batch, 1) if i not in keep]
        time.sleep(6)
    return kept, dropped


def score_offer(offer: dict, description: str, profile: str, cfg: dict, api_key: str) -> dict | None:
    prompt = (
        f"{RUBRIC}\n\n### Candidate profile\n{profile}\n\n### Job offer\n"
        f"Title: {offer['title']}\nCompany: {offer['company']}\nLocation: {offer['location']}\n\n"
        f"{description[:12000]}"
    )
    result = call_gemini(prompt, build_schema(cfg), cfg, api_key)
    if not result:
        return None
    result["score"] = max(0, min(100, int(result.get("score") or 0)))
    result["model"] = cfg["gemini_model"]
    result.update(normalise_salary(result, cfg))
    return result


# ---------------------------------------------------------------- E-mail
def build_email(results: list[dict], csv_text: str | None = None, warning: str = "", waiting: int = 0,
                triaged_out: int = 0, scored: int | None = None) -> str:
    """HTML body of the digest (kept separate from sending so it can be previewed/tested)."""
    colour = {"apply": "#1a7f37", "stretch": "#b08800", "skip": "#cf222e"}
    rows = []
    for r in results:
        facts = " · ".join(x for x in (
            salary_label(r),
            r.get("work_mode") if r.get("work_mode") not in (None, "unknown") else "",
            ", ".join(r.get("contract_types") or []),
            f"EN {r['english_level']}" if r.get("english_level") not in (None, "unknown", "none") else "",
        ) if x)
        tools = ", ".join(r.get("required_tools") or [])
        rows.append(f"""<tr>
          <td style="padding:10px;font-size:20px;font-weight:bold;vertical-align:top;color:{colour.get(r['verdict'], '#333')}">{r['score']}</td>
          <td style="padding:10px">
            <a href="{html.escape(r['url'])}" style="font-weight:bold;font-size:15px">{html.escape(r['title'])}</a><br>
            <span style="color:#555">{html.escape(r['company'])} · {html.escape(r['location'])} · <i>{html.escape(sources.label(r.get('source')))}</i></span><br>
            {f'<span style="color:#333">{html.escape(facts)}</span><br>' if facts else ''}
            {f'<span style="color:#555">Tools: {html.escape(tools)}</span><br>' if tools else ''}
            <i>{html.escape(r['one_line_reason'])}</i><br>
            <span style="color:#1a7f37">+ {html.escape('; '.join(r['strengths']))}</span><br>
            <span style="color:#cf222e">− {html.escape('; '.join(r['gaps']))}</span>
          </td></tr>""")
    parts = []
    if warning:
        parts.append(f"<p style='background:#fff8c5;padding:10px;border-radius:6px'>⚠️ {html.escape(warning)}</p>")
    if results:
        parts.append(f"<h2>{len(results)} new matching offers</h2>"
                     f"<table style='border-collapse:collapse;font-family:Arial,sans-serif'>{''.join(rows)}</table>")
    else:
        done = f" {scored} new offers were scored, none reached the threshold." if scored is not None else ""
        parts.append(f"<p>No new matches today.{done}</p>")
    if triaged_out:
        parts.append(f"<p style='color:#555'>The AI pre-filter skipped {triaged_out} clearly unrelated offers "
                     f"(listed in the weekly report data).</p>")
    if waiting:
        parts.append(f"<p style='color:#555'>{waiting} more offers are waiting in the queue and will be "
                     f"scored on the next run(s).</p>")
    if csv_text:
        parts.append("<p style='color:#555'>Attached: <b>jobs.csv</b> - every scored offer so far "
                     "(ready for Power BI / Excel).</p>")
    return "<div style='font-family:Arial,sans-serif'>" + "".join(parts) + "</div>"


def send_html(subject: str, body_html: str, csv_text: str | None = None) -> None:
    sender, password = os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_APP_PASSWORD")
    if not (sender and password):
        print("E-mail not configured - skipping notification")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"], msg["To"] = sender, os.getenv("EMAIL_TO") or sender
    msg.set_content("Open this e-mail in an HTML-capable client.")
    msg.add_alternative(body_html, subtype="html")
    if csv_text:
        msg.add_attachment(csv_text.encode("utf-8"), maintype="text", subtype="csv",
                           filename=f"jobs_{date.today():%Y-%m-%d}.csv")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print(f"E-mail sent: {subject.split(':')[0]}")


def send_email(results: list[dict], csv_text: str | None = None, warning: str = "", waiting: int = 0,
               triaged_out: int = 0, scored: int | None = None) -> None:
    if results:
        subject = f"Job scan {date.today():%d.%m}: {len(results)} offers, best {results[0]['score']}/100"
    else:
        subject = f"Job scan {date.today():%d.%m}: " + ("check the scraper" if warning else "no new matches")
    send_html(subject, build_email(results, csv_text, warning, waiting, triaged_out, scored), csv_text)


def send_weekly_report(store: Store, cfg: dict, profile: str | None, api_key: str | None,
                       csv_text: str | None = None) -> dict:
    """Compute the weekly stats, ask Gemini for recommendations, e-mail the report."""
    stats = report.compute_stats(store, threshold=cfg["min_score_to_notify"])
    recs = None
    if api_key and profile:
        recs = report.ai_recommendations(
            stats, profile, lambda prompt, schema: call_gemini(prompt, schema, cfg, api_key, temperature=0.4))
    body = report.build_report_html(stats, recs, csv_attached=bool(csv_text))
    send_html(f"Weekly job market report {date.today():%d.%m}: {stats['offers']} offers, "
              f"{stats['near_miss_offers']} near misses", body, csv_text)
    return {"stats": stats, "recs": recs, "html": body}


# ---------------------------------------------------------------- encrypted database
def open_db() -> Store:
    """Decrypt data/jobs.db.enc (if present) and open the database.
    In GitHub Actions a key is mandatory, so an unencrypted database can never end up in the cache."""
    key = os.getenv("DB_ENCRYPTION_KEY")
    if os.getenv("GITHUB_ACTIONS") == "true" and not key:
        sys.exit("Set the DB_ENCRYPTION_KEY secret - the database is only cached encrypted.")
    if key and DB_ENC_FILE.exists():
        decrypt_file(DB_ENC_FILE, DB_FILE, key)
    return Store(DB_FILE)


def close_db(store: Store) -> None:
    """Close the database and, if a key is set, replace it with the encrypted copy."""
    store.close()
    key = os.getenv("DB_ENCRYPTION_KEY")
    if key and DB_FILE.exists():
        encrypt_file(DB_FILE, DB_ENC_FILE, key)
        DB_FILE.unlink()


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, don't notify or save state")
    ap.add_argument("--export", action="store_true", help="write data/jobs.csv from the database and exit")
    ap.add_argument("--report", action="store_true",
                    help="build the weekly report from the database into data/report.html and exit (no e-mail)")
    args = ap.parse_args()

    cfg = load_config()
    if args.export:
        store = open_db()
        CSV_FILE.write_text(store.export_csv(), encoding="utf-8")
        print(f"Exported {store.count()} offers to {CSV_FILE}")
        close_db(store)
        return
    if args.report:
        store = open_db()
        stats = report.compute_stats(store, threshold=cfg["min_score_to_notify"])
        key = os.getenv("GEMINI_API_KEY")
        recs = report.ai_recommendations(
            stats, load_profile(), lambda p, sc: call_gemini(p, sc, cfg, key, temperature=0.4)) if key else None
        REPORT_FILE.write_text(report.build_report_html(stats, recs), encoding="utf-8")
        print(report.report_text(stats, recs))
        print(f"Report written to {REPORT_FILE}")
        close_db(store)
        return

    # Optional overrides for manual test runs (set from the "Run workflow" form)
    if os.getenv("HOURS"):
        cfg["posted_within_hours"] = int(os.getenv("HOURS"))
    if os.getenv("MIN_SCORE"):
        cfg["min_score_to_notify"] = int(os.getenv("MIN_SCORE"))
    ignore_seen = os.getenv("IGNORE_SEEN", "").lower() == "true"
    force_csv = os.getenv("ATTACH_CSV", "").lower() == "true"
    force_report = os.getenv("SEND_REPORT", "").lower() == "true"
    profile = load_profile()
    api_key = os.getenv("GEMINI_API_KEY") or sys.exit("Set GEMINI_API_KEY")

    # dry runs use a throw-away in-memory database, so they never change saved state
    store = Store(Path(":memory:")) if args.dry_run else open_db()
    if not args.dry_run:
        migrated = store.import_legacy_seen(SEEN_FILE)
        if migrated:
            print(f"Migrated {migrated} ids from seen.json into the database")
    backup = os.getenv("BACKUP_RUN", "").lower() == "true"
    if os.getenv("REPORT_ONLY", "").lower() == "true":  # Sunday: weekly report from the database, no scanning
        if backup and store.report_sent_today():
            print("Today's weekly report was already sent - backup run skipped")
        else:
            send_weekly_report(store, cfg, profile, api_key, store.export_csv() if store.count() else None)
            store.mark_report_sent()
        close_db(store)
        return
    if backup and store.ran_today():
        print("Today's main run already happened - backup run skipped")
        close_db(store)
        return
    stats = {"started_at": now_iso()}
    USAGE.update(gemini_calls=0, gemini_errors=0, tokens_in=0, tokens_out=0)
    t0 = time.monotonic()
    budget = float(cfg.get("time_budget_minutes", 40)) * 60  # leave time for e-mails, report and saving the cache

    expired = store.expire_queue(int(cfg.get("queue_max_age_days", 7)))
    backlog = store.queued()  # offers left over from earlier runs go first
    new = fetch_offers(cfg, set() if ignore_seen else store.seen_ids(),
                       set() if ignore_seen else store.recent_fingerprints(), stats)
    t_search = time.monotonic()
    backlog_ids = {o["id"] for o in backlog}
    new = [o for o in new if o["id"] not in backlog_ids]
    new, triaged_out = triage_offers(new, profile, cfg, api_key)
    t_triage = time.monotonic()
    store.save_triaged(triaged_out)  # kept for review; marked as seen so they are not triaged again
    if cfg.get("triage", True):
        print(f"AI pre-filter: kept {len(new)}, dropped {len(triaged_out)}")
    candidates = backlog + new
    cap = int(cfg["max_offers_per_run"])
    offers, overflow = candidates[:cap], candidates[cap:]
    store.enqueue(overflow)  # nothing over the cap is lost - it is scored on the next run
    print(f"From queue: {len(backlog)}, new: {len(new)}, scoring now: {len(offers)}, "
          f"queued for next run: {len(overflow)}, expired from queue: {expired}")

    results, failed, stopped_by_time = [], 0, 0
    detail_failed: dict[str, int] = {}
    for i, o in enumerate(offers, 1):
        if time.monotonic() - t0 > budget:
            store.enqueue(offers[i - 1:])  # the rest waits for the next run instead of risking the job timeout
            stopped_by_time = len(offers) - i + 1
            print(f"Time budget of {budget / 60:.0f} min reached - {stopped_by_time} offers moved to the queue")
            break
        src = sources.ALL.get(o.get("source") or "linkedin", sources.linkedin)
        try:
            desc = src.detail(o)
        except Exception:  # unexpected page format - treat like a temporary failure
            desc = None
        if desc is None:  # network problem / rate limit - try again next run
            store.queue_failed(o)
            failed += 1
            detail_failed[src.NAME] = detail_failed.get(src.NAME, 0) + 1
            continue
        if not desc:  # offer removed or no description - nothing to score
            store.dequeue(o["id"])
            store.mark_seen(o["id"])
            continue
        s = score_offer(o, desc, profile, cfg, api_key)
        if s:
            row = {**o, **s, "first_seen": now_iso()}
            store.save_offer(row, desc)  # every scored offer is kept, not only the good ones
            store.dequeue(o["id"])
            results.append(row)
            if args.dry_run:
                print(f"[{i}/{len(offers)}] {s['score']:>3}  {o['title']} – {o['company']} ({o['location']})")
        else:
            store.queue_failed(o)  # retried on the next run, dropped after 3 failed attempts
            failed += 1
        time.sleep(6)  # stay under the free-tier requests-per-minute limit
    waiting = store.queue_size()
    t_score = time.monotonic()
    timing = {"search_seconds": round(t_search - t0), "triage_seconds": round(t_triage - t_search),
              "score_seconds": round(t_score - t_triage)}
    if stats.get("sources"):  # add description-fetch failures to the per-portal stats
        per = json.loads(stats["sources"])
        for name, n in detail_failed.items():
            per.setdefault(name, {})["detail_failed"] = n
        stats["sources"] = json.dumps(per)
    print(f"Gemini: {USAGE['gemini_calls']} calls, {USAGE['gemini_errors']} failed, "
          f"{USAGE['tokens_in']:,} tokens in / {USAGE['tokens_out']:,} out; "
          f"search {timing['search_seconds']}s, pre-filter {timing['triage_seconds']}s, "
          f"scoring {timing['score_seconds']}s")

    good = sorted((r for r in results if r["score"] >= cfg["min_score_to_notify"]),
                  key=lambda r: r["score"], reverse=True)
    print(f"Scored: {len(results)}, failed: {failed}, above threshold: {len(good)}, "
          f"in database: {store.count()}, waiting in queue: {waiting}")  # counts only - logs may be public

    # LinkedIn returning nothing at all almost always means the page layout changed or we got blocked
    warning = ""
    if stats.get("searches") and not stats.get("cards_found"):
        warning = ("The scraper found 0 offers across all sources - the portals may have changed their "
                   "page layout or blocked the requests. Check the latest Actions log.")
    elif stats.get("failed_sources"):
        warning = (f"Searching {', '.join(map(sources.label, stats['failed_sources']))} failed - "
                   f"its page format may have changed. The other sources worked.")
    stats.pop("failed_sources", None)

    if args.dry_run:
        for r in good:
            print(f"\n{r['score']}/100 {r['verdict'].upper()} – {r['title']} – {r['company']}\n{r['url']}")
            if salary_label(r):
                print(f"  {salary_label(r)} · {r.get('work_mode')}")
            print("  + " + "\n  + ".join(r["strengths"]))
            print("  - " + "\n  - ".join(r["gaps"]))
        if warning:
            print("WARNING: " + warning)
        store.close()
        return

    weekday = datetime.now().weekday()
    attach = force_csv or weekday in (cfg.get("attach_csv_weekdays") or [])
    report_due = force_report or weekday in (cfg.get("report_weekdays") or [])
    csv_text = store.export_csv() if attach and store.count() else None
    # On report days the CSV travels with the weekly report instead of the daily digest
    digest_csv = None if report_due else csv_text
    # By default a digest goes out after every run, even with no matches - so a missing e-mail always means
    # the run didn't happen (GitHub can drop scheduled runs) rather than "nothing new today".
    if good or digest_csv or warning or cfg.get("email_every_run", True):
        send_email(good, digest_csv, warning, waiting, len(triaged_out), len(results))
        store.mark_notified([r["id"] for r in good])
    if report_due:
        send_weekly_report(store, cfg, profile, api_key, csv_text)

    store.save_run({**stats, "finished_at": now_iso(), "scored": len(results), "failed": failed,
                    "notified": len(good), "model": cfg["gemini_model"],
                    "queued": waiting, "queue_expired": expired, "triaged_out": len(triaged_out),
                    "stopped_by_time": stopped_by_time, **timing, **USAGE})
    close_db(store)


if __name__ == "__main__":
    main()
