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
import math
from datetime import date, datetime, timezone
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

one_line_reason: at most ~15 words, the main reason for the score; don't start with "The candidate".
Strengths: at most 4, each a short phrase (max ~6 words).

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


def pick_available_model(api_key: str, exclude: set | None = None) -> str | None:
    """Last resort when every configured model is gone: newest *lite* Flash model (generous free tier).
    Non-lite models are never picked - on the free tier some allow only ~20 requests a day."""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": api_key, "pageSize": 200}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        print(f"  Could not list models: {r.status_code}", file=sys.stderr)
        return None
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    lite = [n for n in names if "flash-lite" in n and n not in (exclude or set())
            and not any(x in n for x in ("preview", "exp", "tts", "image", "live", "audio"))]
    lite.sort(key=lambda n: [-int(x) for x in re.findall(r"\d+", n)[:2]] or [0])
    print(f"  Available lite models: {', '.join(lite[:5]) or 'none'}")
    return lite[0] if lite else None


# Gemini usage of the current run, written to the runs table (monitoring of cost and reliability)
USAGE = {"gemini_calls": 0, "gemini_errors": 0, "tokens_in": 0, "tokens_out": 0}
DEAD_MODELS: set[str] = set()   # models that hit their daily quota / don't exist - skipped for the rest of the run
DEFAULT_MODELS = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def _gemini_failed() -> None:
    USAGE["gemini_errors"] += 1
    return None


def _models(cfg: dict) -> list[str]:
    models = list(cfg.get("gemini_models") or [cfg.get("gemini_model")] + DEFAULT_MODELS)
    return [m for m in dict.fromkeys(m for m in models if m) if m not in DEAD_MODELS]


def _google_error(r) -> tuple[str, str, float | None]:
    """(status, short message, retry delay in s) from a Gemini error response - safe to log (no prompt/key)."""
    try:
        err = r.json().get("error", {})
        if not isinstance(err, dict):
            raise ValueError
    except Exception:
        return "", str(getattr(r, "text", ""))[:120], None
    quota, delay = "", None
    for d in err.get("details", []):
        for v in d.get("violations", []):
            quota = v.get("quotaId", "") or quota
        if d.get("retryDelay"):
            try:
                delay = float(str(d["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    msg = re.sub(r"\s+", " ", str(err.get("message", "")))[:120]
    return err.get("status", ""), (f"{quota}: " if quota else "") + msg, delay


def call_gemini(prompt: str, schema: dict, cfg: dict, api_key: str, temperature: float = 0.2) -> dict | None:
    """One structured-output call. Tries the configured models in order:
    - 503/500/timeout (overloaded): short wait, then the next model for this call;
    - 429 per-day quota: the model is dropped for the rest of the run, next model immediately;
    - 429 per-minute: waits the delay Google asks for (max 60 s);
    - 404: model retired - dropped, next model.
    Every failure is logged with Google's status (no prompt, no key). Returns the parsed JSON or None."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    rotate = 0
    for attempt in range(6):
        models = _models(cfg)
        if not models:
            extra = pick_available_model(api_key, exclude=DEAD_MODELS)
            if not extra:
                print("  No Gemini model available - update gemini_models in config.yaml", file=sys.stderr)
                return _gemini_failed()
            print(f"  All configured models unavailable - trying {extra}")
            cfg["gemini_models"] = [extra]
            models = [extra]
        model = models[rotate % len(models)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, params={"key": api_key}, json=body, timeout=90)
        except requests.RequestException as e:  # timeout / connection drop - try the next model
            print(f"  Gemini {model}: {type(e).__name__}", file=sys.stderr)
            rotate += 1
            time.sleep(3)
            continue
        if r.status_code != 200:
            status, msg, delay = _google_error(r)
            print(f"  Gemini {model}: {r.status_code} {status} {msg}", file=sys.stderr)
            if r.status_code == 429 and "PerDay" in msg:
                DEAD_MODELS.add(model)       # daily quota gone - waiting won't help today
                continue
            if r.status_code == 429:
                time.sleep(min(delay or 20, 60))
                continue
            if r.status_code == 404:
                DEAD_MODELS.add(model)
                continue
            if r.status_code in (500, 503, 504):
                rotate += 1
                time.sleep(3 * (attempt + 1))
                continue
            return _gemini_failed()
        try:
            data = r.json()
            usage = data.get("usageMetadata") or {}
            USAGE["gemini_calls"] += 1
            USAGE["tokens_in"] += int(usage.get("promptTokenCount") or 0)
            USAGE["tokens_out"] += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
            result = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            cfg["_last_model"] = model
            return result
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
    result["model"] = cfg.get("_last_model") or _models(cfg)[0]
    result.update(normalise_salary(result, cfg))
    return result


# ---------------------------------------------------------------- E-mail
def _chip(text: str) -> str:
    return (f"<span style='display:inline-block;font-size:12px;line-height:16px;color:{report.INK2};background:#f1efeb;"
            f"border-radius:10px;padding:3px 9px;margin:0 4px 4px 0;white-space:nowrap'>{html.escape(text)}</span>")


def _offer_card(r: dict, threshold: int) -> str:
    strong = r["score"] >= max(75, threshold)
    col, label = ("#1a7f37", "Strong match") if strong else ("#9a6700", "Worth a look")
    facts = [salary_label(r),
             r.get("work_mode") if r.get("work_mode") not in (None, "unknown") else "",
             ", ".join(r.get("contract_types") or []),
             f"EN {r['english_level']}" if r.get("english_level") not in (None, "unknown", "none") else ""]
    chips = "".join(_chip(f) for f in facts if f)
    plus = "".join(f"<div style='margin:2px 0;padding-left:16px;text-indent:-16px'><span style='color:#1a7f37'>✓</span> {html.escape(x)}</div>"
                   for x in (r.get("strengths") or [])[:4])
    minus = "".join(f"<div style='margin:2px 0;padding-left:16px;text-indent:-16px'><span style='color:#b42318'>✗</span> {html.escape(x)}</div>"
                    for x in (r.get("gaps") or [])[:4])
    url = html.escape(r.get("url") or "")
    where = " · ".join(html.escape(x) for x in (r.get("company"), r.get("location"), sources.label(r.get("source"))) if x)
    return (
        f"<tr><td style='padding:0 0 12px'><table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='background:#ffffff;border:1px solid {report.LINE};border-radius:10px'><tr><td style='padding:16px 18px'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'><tr><td style='vertical-align:top'>"
        f"<div style='font-size:12px;color:{col};font-weight:600'>{label}</div>"
        f"<a href='{url}' style='font-size:16px;line-height:21px;font-weight:600;color:{report.INK};text-decoration:none'>"
        f"{html.escape(r.get('title') or '')}</a>"
        f"<div style='font-size:13px;color:{report.INK2};margin-top:2px'>{where}</div></td>"
        f"<td style='vertical-align:top;width:60px;text-align:right'><div style='display:inline-block;width:50px;height:50px;"
        f"line-height:50px;border-radius:25px;border:3px solid {col};font-size:18px;font-weight:700;color:{col};"
        f"text-align:center'>{r['score']}</div></td></tr></table>"
        + (f"<div style='margin-top:10px'>{chips}</div>" if chips else "")
        + f"<div style='font-size:13px;line-height:19px;color:{report.INK};margin:6px 0 10px'>"
          f"{html.escape(r.get('one_line_reason') or '')}</div>"
          f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='font-size:13px;line-height:18px;"
          f"color:{report.INK2}'><tr><td width='50%' style='vertical-align:top;padding-right:10px'>{plus}</td>"
          f"<td width='50%' style='vertical-align:top'>{minus}</td></tr></table>"
          f"<div style='margin-top:12px'><a href='{url}' style='display:inline-block;background:{report.ACCENT};color:#ffffff;"
          f"font-size:13px;font-weight:600;text-decoration:none;border-radius:6px;padding:7px 14px'>View offer</a></div>"
          f"</td></tr></table></td></tr>")


def build_email(results: list[dict], csv_text: str | None = None, warning: str = "", waiting: int = 0,
                triaged_out: int = 0, scored: int | None = None, threshold: int = 60) -> str:
    """HTML body of the daily digest - e-mail-safe cards (kept separate from sending so it can be previewed/tested)."""
    INK, INK2, MUTED, PAGE = report.INK, report.INK2, report.MUTED, report.PAGE
    if results:
        title = f"{len(results)} new matching offer{'s' if len(results) != 1 else ''}"
    else:
        title = "No new matches today"
    sub = [f"{date.today():%A} {date.today().day} {date.today():%B}"]
    if scored is not None:
        sub.append(f"{scored} offers scored")
    if results:
        sub.append(f"best {results[0]['score']}/100")
    blocks = []
    if warning:
        blocks.append(f"<tr><td style='padding:0 0 12px'><div style='background:#fff8c5;border:1px solid #eedc82;"
                      f"border-radius:10px;padding:12px 16px;font-size:13px;line-height:19px;color:{INK}'>⚠️ "
                      f"{html.escape(warning)}</div></td></tr>")
    if results:
        blocks += [_offer_card(r, threshold) for r in results]
    else:
        blocks.append(f"<tr><td style='padding:0 0 12px'><div style='background:#ffffff;border:1px solid {report.LINE};"
                      f"border-radius:10px;padding:16px 18px;font-size:13px;line-height:19px;color:{INK2}'>Nothing reached "
                      f"the threshold this time – the scanner ran normally.</div></td></tr>")
    foot = []
    if waiting:
        foot.append(f"{waiting} offers waiting in the queue")
    if triaged_out:
        foot.append(f"AI pre-filter skipped {triaged_out} unrelated offers")
    if csv_text:
        foot.append("full dataset attached as jobs.csv")
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<meta name='color-scheme' content='light only'><meta name='supported-color-schemes' content='light'></head>"
        f"<body style='margin:0;padding:0;background:{PAGE}'><table role='presentation' width='100%' cellpadding='0' "
        f"cellspacing='0' style='background:{PAGE}'><tr><td align='center' style='padding:24px 12px'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:640px;"
        f"font-family:{report.FONT};color:{INK}'>"
        f"<tr><td style='padding:0 2px 14px'><div style='font-size:12px;letter-spacing:.04em;text-transform:uppercase;"
        f"color:{MUTED}'>Job scanner · daily digest</div><div style='font-size:22px;line-height:28px;font-weight:600;"
        f"color:{INK};margin-top:2px'>{title}</div><div style='font-size:13px;color:{INK2};margin-top:2px'>"
        f"{' · '.join(sub)}</div></td></tr>" + "".join(blocks)
        + (f"<tr><td style='padding:6px 2px 0;font-size:12px;color:{MUTED};text-align:center'>{' · '.join(foot)}</td></tr>"
           if foot else "")
        + "</table></td></tr></table></body></html>")


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
               triaged_out: int = 0, scored: int | None = None, threshold: int = 60) -> None:
    if results:
        subject = f"Job scan {date.today():%d.%m}: {len(results)} offers, best {results[0]['score']}/100"
    else:
        subject = f"Job scan {date.today():%d.%m}: " + ("check the scraper" if warning else "no new matches")
    send_html(subject, build_email(results, csv_text, warning, waiting, triaged_out, scored, threshold), csv_text)


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
    global _OPEN_STORE
    _OPEN_STORE = Store(DB_FILE)
    return _OPEN_STORE


_OPEN_STORE: Store | None = None


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
    manual_hours = os.getenv("HOURS", "") not in ("", "auto")  # "auto" = external trigger, like a schedule
    if manual_hours:
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
    if not manual_hours:  # scheduled run: look back to the last full scan, so a skipped day loses nothing
        last = store.last_full_scan_start()
        if last:
            gap = math.ceil((datetime.now(timezone.utc) - last).total_seconds() / 3600) + 2  # 2 h overlap
            cap = int(cfg.get("max_lookback_hours", 96))
            cfg["posted_within_hours"] = min(max(int(cfg["posted_within_hours"]), gap), cap)
            if cfg["posted_within_hours"] > 26:
                print(f"Last full scan {gap - 2} h ago - looking back {cfg['posted_within_hours']} h")
    stats = {"started_at": now_iso(), "window_hours": int(cfg["posted_within_hours"])}
    USAGE.update(gemini_calls=0, gemini_errors=0, tokens_in=0, tokens_out=0)
    DEAD_MODELS.clear()
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

    results, failed, stopped_by_time, gemini_down = [], 0, 0, False
    detail_failed: dict[str, int] = {}
    fails_in_row = 0
    for i, o in enumerate(offers, 1):
        if fails_in_row >= int(cfg.get("max_gemini_failures_in_row", 4)):
            # Gemini is down or out of quota - don't burn the whole run on retries; try again next run
            store.enqueue(offers[i - 1:])
            gemini_down = True
            print(f"Gemini failed {fails_in_row} offers in a row - stopping, {len(offers) - i + 1} offers queued")
            break
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
            fails_in_row = 0
            if args.dry_run:
                print(f"[{i}/{len(offers)}] {s['score']:>3}  {o['title']} – {o['company']} ({o['location']})")
        else:
            store.enqueue([o])  # Gemini failure is not the offer's fault - keep it without counting an attempt
            failed += 1
            fails_in_row += 1
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
    elif gemini_down or (USAGE["gemini_errors"] and not results and offers):
        warning = ("Gemini was unavailable (overloaded or out of quota), so no offers could be scored. "
                   "They are kept in the queue and will be scored on the next run.")
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
    email_error = None
    if good or digest_csv or warning or cfg.get("email_every_run", True):
        try:
            send_email(good, digest_csv, warning, waiting, len(triaged_out), len(results), cfg["min_score_to_notify"])
            store.mark_notified([r["id"] for r in good])
        except Exception as e:  # scoring results are saved anyway; the job is failed below so it shows up
            email_error = e
            print(f"E-mail failed: {type(e).__name__}: {e}", file=sys.stderr)
    if report_due:
        try:
            send_weekly_report(store, cfg, profile, api_key, csv_text)
        except Exception as e:
            email_error = email_error or e
            print(f"Weekly report failed: {type(e).__name__}: {e}", file=sys.stderr)

    store.save_run({**stats, "finished_at": now_iso(), "scored": len(results), "failed": failed,
                    "notified": len(good), "model": cfg.get("_last_model"),
                    "queued": waiting, "queue_expired": expired, "triaged_out": len(triaged_out),
                    "stopped_by_time": stopped_by_time, **timing, **USAGE})
    close_db(store)
    if email_error:
        sys.exit(f"Run finished and saved, but the e-mail failed: {email_error}")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Whatever happened, encrypt what was saved so far - the workflow caches it even after a failure,
        # so a crash at the end (e.g. e-mail) never throws away a whole run of scoring.
        if _OPEN_STORE is not None and DB_FILE.exists():
            close_db(_OPEN_STORE)
