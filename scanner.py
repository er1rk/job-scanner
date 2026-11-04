"""
AI Job Scanner
--------------
1. Pulls fresh job offers from LinkedIn's public job search (no login).
2. Filters out obvious mismatches by title.
3. Asks Google Gemini to score each offer against the candidate profile
   using a recruiter-style rubric (0-100).
4. E-mails the best matches and remembers what it has already seen.

Run locally:   python scanner.py --dry-run      (prints results, sends nothing)
Run for real:  python scanner.py                (needs env vars, see README)
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
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "data" / "seen.json"
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
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.parent.mkdir(exist_ok=True)
    # keep the file small: last 3000 ids are plenty for de-duplication
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:]))


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


def fetch_offers(cfg: dict, seen: set) -> list[dict]:
    secs = int(cfg["posted_within_hours"]) * 3600
    excluded = [w.lower() for w in cfg.get("exclude_title_keywords", [])]
    found: dict[str, dict] = {}
    for s in cfg["searches"]:
        for page in range(int(cfg.get("pages_per_search", 1))):
            url = SEARCH_URL.format(kw=quote_plus(s["keywords"]), loc=quote_plus(s["location"]),
                                    secs=secs, start=page * 25)
            html = get(url)
            if not html:
                break
            for o in parse_search(html):
                if o["id"] in seen or o["id"] in found:
                    continue
                if any(w in o["title"].lower() for w in excluded):
                    continue
                found[o["id"]] = o
            time.sleep(2)
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
Never assume skills the profile does not state. Answer in English."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER"},
        "verdict": {"type": "STRING", "enum": ["apply", "stretch", "skip"]},
        "seniority": {"type": "STRING"},
        "strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
        "gaps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "one_line_reason": {"type": "STRING"},
    },
    "required": ["score", "verdict", "strengths", "gaps", "one_line_reason"],
}


def pick_available_model(api_key: str) -> str | None:
    """If the configured model doesn't exist for this key, find a Flash model that does."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": api_key, "pageSize": 200}, timeout=30)
    if r.status_code != 200:
        print(f"  Could not list models: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return None
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if "flash" in n and not any(x in n for x in ("tts", "image", "live", "embedding"))]
    # prefer stable over preview, newest first
    flash.sort(key=lambda n: ("preview" in n or "exp" in n, [-int(x) for x in re.findall(r"\d+", n)[:2]]))
    print(f"  Available Flash models: {', '.join(flash[:6]) or 'none'}")
    return flash[0] if flash else None


def score_offer(offer: dict, description: str, profile: str, cfg: dict, api_key: str) -> dict | None:
    prompt = (
        f"{RUBRIC}\n\n### Candidate profile\n{profile}\n\n### Job offer\n"
        f"Title: {offer['title']}\nCompany: {offer['company']}\nLocation: {offer['location']}\n\n"
        f"{description[:12000]}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    for attempt in range(3):
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


# ---------------------------------------------------------------- E-mail
def send_email(results: list[dict]) -> None:
    sender, password = os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_APP_PASSWORD")
    if not (sender and password):
        print("E-mail not configured - skipping notification")
        return
    to = os.getenv("EMAIL_TO") or sender
    colour = {"apply": "#1a7f37", "stretch": "#b08800", "skip": "#cf222e"}
    rows = "".join(
        f"""<tr>
          <td style="padding:10px;font-size:20px;font-weight:bold;color:{colour.get(r['verdict'], '#333')}">{r['score']}</td>
          <td style="padding:10px">
            <a href="{r['url']}" style="font-weight:bold;font-size:15px">{html.escape(r['title'])}</a><br>
            <span style="color:#555">{html.escape(r['company'])} · {html.escape(r['location'])}</span><br>
            <i>{html.escape(r['one_line_reason'])}</i><br>
            <span style="color:#1a7f37">+ {html.escape('; '.join(r['strengths']))}</span><br>
            <span style="color:#cf222e">− {html.escape('; '.join(r['gaps']))}</span>
          </td></tr>"""
        for r in results
    )
    body = (f"<h2>{len(results)} new matching offers</h2>"
            f"<table style='border-collapse:collapse;font-family:Arial,sans-serif'>{rows}</table>")
    msg = EmailMessage()
    msg["Subject"] = f"Job scan {date.today():%d.%m}: {len(results)} offers, best {results[0]['score']}/100"
    msg["From"], msg["To"] = sender, to
    msg.set_content("Open this e-mail in an HTML-capable client.")
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print("E-mail sent")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, don't notify or save state")
    args = ap.parse_args()

    cfg = load_config()
    # Optional overrides for manual test runs (set from the "Run workflow" form)
    if os.getenv("HOURS"):
        cfg["posted_within_hours"] = int(os.getenv("HOURS"))
    if os.getenv("MIN_SCORE"):
        cfg["min_score_to_notify"] = int(os.getenv("MIN_SCORE"))
    ignore_seen = os.getenv("IGNORE_SEEN", "").lower() == "true"
    profile = load_profile()
    api_key = os.getenv("GEMINI_API_KEY") or sys.exit("Set GEMINI_API_KEY")
    seen = load_seen()

    offers = fetch_offers(cfg, set() if ignore_seen else seen)
    print(f"New offers to score: {len(offers)}")

    results = []
    for i, o in enumerate(offers, 1):
        html = get(DETAIL_URL.format(id=o["id"]))
        desc = parse_detail(html) if html else ""
        if not desc:
            seen.add(o["id"])
            continue
        s = score_offer(o, desc, profile, cfg, api_key)
        if s:
            seen.add(o["id"])  # only remember offers that were actually scored
            results.append({**o, **s})
            if args.dry_run:
                print(f"[{i}/{len(offers)}] {s['score']:>3}  {o['title']} – {o['company']} ({o['location']})")
        time.sleep(6)  # stay under the free-tier requests-per-minute limit

    good = sorted((r for r in results if r["score"] >= cfg["min_score_to_notify"]),
                  key=lambda r: r["score"], reverse=True)
    print(f"Scored: {len(results)}, above threshold: {len(good)}")  # counts only - logs may be public

    if args.dry_run:
        for r in good:
            print(f"\n{r['score']}/100 {r['verdict'].upper()} – {r['title']} – {r['company']}\n{r['url']}")
            print("  + " + "\n  + ".join(r["strengths"]))
            print("  - " + "\n  - ".join(r["gaps"]))
        return

    if good:
        send_email(good)
    save_seen(seen)


if __name__ == "__main__":
    main()
