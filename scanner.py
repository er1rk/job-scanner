"""
AI Job Scanner
--------------
1. Pulls fresh job offers from LinkedIn's public job search (no login).
2. Filters out obvious mismatches by title.
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
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

import report
from storage import Store, decrypt_file, encrypt_file, now_iso

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "data" / "seen.json"   # legacy cache, migrated into the database on first run
DB_FILE = ROOT / "data" / "jobs.db"
DB_ENC_FILE = ROOT / "data" / "jobs.db.enc"   # the only form that is cached in GitHub Actions
CSV_FILE = ROOT / "data" / "jobs.csv"
REPORT_FILE = ROOT / "data" / "report.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}
SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={kw}&location={loc}&f_TPR=r{secs}&start={start}"
)
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}/"


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


def get(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:  # rate limited - back off
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(5)
    return None


# ---------------------------------------------------------------- LinkedIn
def parse_search(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    for card in soup.select("div.base-card[data-entity-urn]"):
        m = re.search(r"jobPosting:(\d+)", card["data-entity-urn"])
        if not m:
            continue
        def txt(sel):
            el = card.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""
        offers.append({
            "id": m.group(1),
            "title": txt("h3.base-search-card__title"),
            "company": txt("h4.base-search-card__subtitle"),
            "location": txt("span.job-search-card__location"),
            "url": VIEW_URL.format(id=m.group(1)),
        })
    return offers


def parse_detail(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.select_one("div.show-more-less-html__markup")
    text = desc.get_text("\n", strip=True) if desc else ""
    criteria = [
        f"{li.select_one('h3').get_text(strip=True)}: {li.select_one('span').get_text(strip=True)}"
        for li in soup.select("li.description__job-criteria-item")
        if li.select_one("h3") and li.select_one("span")
    ]
    return (text + "\n\n" + "\n".join(criteria)).strip()


def build_searches(cfg: dict) -> list[dict]:
    """Every keyword x every location, plus remote-in-Poland, plus any explicit 'searches'."""
    searches = list(cfg.get("searches") or [])
    for kw in cfg.get("keywords", []):
        for loc in cfg.get("locations", []):
            searches.append({"keywords": kw, "location": loc})
        if cfg.get("include_remote_poland"):
            searches.append({"keywords": kw, "location": "Poland", "remote": True})
    return searches


def fetch_offers(cfg: dict, seen: set, stats: dict) -> list[dict]:
    secs = int(cfg["posted_within_hours"]) * 3600
    excluded = [w.lower() for w in cfg.get("exclude_title_keywords", [])]
    found: dict[str, dict] = {}
    cards_total, filtered = 0, set()
    for s in build_searches(cfg):
        for page in range(int(cfg.get("pages_per_search", 1))):
            url = SEARCH_URL.format(kw=quote_plus(s["keywords"]), loc=quote_plus(s["location"]),
                                    secs=secs, start=page * 25)
            if s.get("remote"):
                url += "&f_WT=2"  # LinkedIn filter: remote only
            html = get(url)
            if not html:
                break
            cards = parse_search(html)
            cards_total += len(cards)
            for o in cards:
                if o["id"] in seen or o["id"] in found:
                    continue
                if any(w in o["title"].lower() for w in excluded):
                    filtered.add(o["id"])
                    continue
                o["search_keyword"] = s["keywords"]
                found[o["id"]] = o
            time.sleep(2)
            if len(cards) < 25:  # no more pages
                break
    stats.update(searches=len(build_searches(cfg)), cards_found=cards_total,
                 filtered_title=len(filtered), new_offers=len(found))
    print(f"Searches run: {stats['searches']}, cards: {cards_total}, "
          f"dropped by title: {len(filtered)}, unique new offers: {len(found)}")
    return list(found.values())[: int(cfg["max_offers_per_run"])]


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
            return None
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError):
            return None
    return None


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
def build_email(results: list[dict], csv_text: str | None = None, warning: str = "") -> str:
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
            <span style="color:#555">{html.escape(r['company'])} · {html.escape(r['location'])}</span><br>
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
        parts.append("<p>No new offers above the threshold today.</p>")
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


def send_email(results: list[dict], csv_text: str | None = None, warning: str = "") -> None:
    if results:
        subject = f"Job scan {date.today():%d.%m}: {len(results)} offers, best {results[0]['score']}/100"
    else:
        subject = f"Job scan {date.today():%d.%m}: " + ("check the scraper" if warning else "data export")
    send_html(subject, build_email(results, csv_text, warning), csv_text)


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
    stats = {"started_at": now_iso()}

    offers = fetch_offers(cfg, set() if ignore_seen else store.seen_ids(), stats)
    print(f"New offers to score: {len(offers)}")

    results, failed = [], 0
    for i, o in enumerate(offers, 1):
        page = get(DETAIL_URL.format(id=o["id"]))
        desc = parse_detail(page) if page else ""
        if not desc:
            store.mark_seen(o["id"])
            continue
        s = score_offer(o, desc, profile, cfg, api_key)
        if s:
            row = {**o, **s, "first_seen": now_iso()}
            store.save_offer(row, desc)  # every scored offer is kept, not only the good ones
            results.append(row)
            if args.dry_run:
                print(f"[{i}/{len(offers)}] {s['score']:>3}  {o['title']} – {o['company']} ({o['location']})")
        else:
            failed += 1  # not saved -> will be retried on the next run
        time.sleep(6)  # stay under the free-tier requests-per-minute limit

    good = sorted((r for r in results if r["score"] >= cfg["min_score_to_notify"]),
                  key=lambda r: r["score"], reverse=True)
    print(f"Scored: {len(results)}, failed: {failed}, above threshold: {len(good)}, "
          f"in database: {store.count()}")  # counts only - logs may be public

    # LinkedIn returning nothing at all almost always means the page layout changed or we got blocked
    warning = ""
    if stats.get("searches") and not stats.get("cards_found"):
        warning = ("The scraper found 0 offers across all searches - LinkedIn may have changed its "
                   "page layout or blocked the request. Check the latest Actions log.")

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
    if good or digest_csv or warning:
        send_email(good, digest_csv, warning)
        store.mark_notified([r["id"] for r in good])
    if report_due:
        send_weekly_report(store, cfg, profile, api_key, csv_text)

    store.save_run({**stats, "finished_at": now_iso(), "scored": len(results), "failed": failed,
                    "notified": len(good), "model": cfg["gemini_model"]})
    close_db(store)


if __name__ == "__main__":
    main()
