"""
Tailored CV (PDF) + answers to typical application-form questions for one offer.

Facts come only from the master CV (CV_MASTER secret / git-ignored cv_master.yaml) and the candidate
profile. Gemini picks, orders and rewords them for the offer; the code fixes everything that must not
change (names, titles, companies, dates, education, certificates) and renders a one-page A4 PDF in the
layout of the master CV.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
FONTS = ROOT / "cv" / "fonts"
ANSWER_QUESTIONS = [
    "Why are you interested in this role and company?",
    "Why are you a good fit for this role?",
    "What are your salary expectations?",
    "When can you start? / What is your notice period?",
]

PROMPT = """You tailor a one-page CV and short application answers for ONE job offer.

HONESTY - the most important rule:
- Use ONLY facts from the master CV and the candidate profile below. Never invent tools, numbers, duties,
  results or experience. You may reword, merge, shorten and reorder - and you may turn a stated fact into a
  bullet - but every claim must be traceable to the master CV or the profile.
- Keep skill levels honest (e.g. "foundational SQL" stays foundational).

CV content:
- profile: 3-4 sentences (max 70 words), aimed at this offer's main requirements, in the style of the master
  profile. No first person, no buzzwords.
- experience: for every role id give 1-5 bullets (most relevant first). The role closest to the offer gets the
  most bullets; unrelated roles get 1. Each bullet max ~25 words, starts with a past-tense verb, uses the offer's
  vocabulary where it truthfully applies.
- project_bullets: 1-2 bullets for each project (same order as the master).
- skills: the master skill labels in the order most relevant to the offer; for each, the items most relevant
  first (you may drop irrelevant items, never add new ones).
- coursework: up to 4 items from the master coursework of the first degree, most relevant first.

Answers (English, first person, natural and specific to this company and offer; 60-110 words each, except the
practical ones):
{questions}
- Salary: {salary}
- Start / notice period: {availability}
Also answer any other question the offer explicitly asks applicants (max 2 more).

### Master CV
{master}

### Candidate profile
{profile}

### Job offer
Title: {title}
Company: {company}
Location: {location}

{description}"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "profile": {"type": "STRING"},
        "experience": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"}, "bullets": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["id", "bullets"]}},
        "project_bullets": {"type": "ARRAY", "items": {"type": "ARRAY", "items": {"type": "STRING"}}},
        "skills": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "label": {"type": "STRING"}, "items": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["label", "items"]}},
        "coursework": {"type": "ARRAY", "items": {"type": "STRING"}},
        "answers": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "question": {"type": "STRING"}, "answer": {"type": "STRING"}}, "required": ["question", "answer"]}},
    },
    "required": ["profile", "experience", "project_bullets", "skills", "coursework", "answers"],
}


def load_master() -> dict | None:
    text = os.getenv("CV_MASTER")
    if not text and (ROOT / "cv_master.yaml").exists():
        text = (ROOT / "cv_master.yaml").read_text(encoding="utf-8")
    if not text:
        return None
    m = yaml.safe_load(text)
    return m if isinstance(m, dict) and m.get("name") and m.get("experience") else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def merge(master: dict, t: dict) -> dict:
    """Fixed parts from the master, chosen/reworded parts from the model - with fallbacks to the master."""
    by_id = {e.get("id"): [b.strip() for b in e.get("bullets") or [] if b and b.strip()]
             for e in t.get("experience") or []}
    roles = sorted(master["experience"], key=lambda e: (str(e.get("end") or "9999"), str(e.get("start") or "")),
                   reverse=True)  # most recent first
    experience = [{**e, "bullets": (by_id.get(e.get("id")) or e.get("bullets") or [])[:5]} for e in roles]
    projects = []
    for i, p in enumerate(master.get("projects") or []):
        chosen = (t.get("project_bullets") or [])[i:i + 1]
        projects.append({**p, "bullets": ([b for b in chosen[0] if b.strip()] if chosen else None)
                         or p.get("bullets") or []})
    master_skills = {k: [str(x) for x in v] for k, v in (master.get("skills") or {}).items()}
    known = {_norm(x): x for v in master_skills.values() for x in v}
    skills, used = [], set()
    for row in t.get("skills") or []:
        label = row.get("label")
        if label in master_skills and label not in used:
            items = [known[_norm(x)] for x in row.get("items") or [] if _norm(x) in known]
            skills.append((label, items or master_skills[label]))
            used.add(label)
    skills += [(k, v) for k, v in master_skills.items() if k not in used]  # never lose a skill row
    education = [dict(e) for e in master.get("education") or []]
    if education and education[0].get("coursework"):
        allowed = {_norm(c): c for c in education[0]["coursework"]}
        picked = [allowed[_norm(c)] for c in t.get("coursework") or [] if _norm(c) in allowed]
        education[0]["coursework"] = list(dict.fromkeys(picked))[:4] or education[0]["coursework"]
    return {"name": master["name"], "contact": master.get("contact") or [],
            "profile": (t.get("profile") or "").strip() or master.get("profile", ""),
            "experience": experience, "projects": projects, "skills": skills, "education": education,
            "certificates": master.get("certificates") or [], "footer": master.get("footer") or ""}


CSS = """
@font-face {font-family: Carlito; src: url("%(f)s/Carlito-Regular.ttf");}
@font-face {font-family: Carlito; font-weight: bold; src: url("%(f)s/Carlito-Bold.ttf");}
@font-face {font-family: Carlito; font-style: italic; src: url("%(f)s/Carlito-Italic.ttf");}
@page {size: A4; margin: 15mm 18mm 12mm;}
body {font-family: Carlito, sans-serif; font-size: %(fs).2fpt; line-height: 1.28; color: #1f1f1f; margin: 0;}
h1 {font-size: 19pt; margin: 0 0 2pt;}
.contact {font-size: %(fs).2fpt;}
h2 {font-size: %(fs).2fpt; letter-spacing: .32em; margin: %(gap).1fpt 0 4pt; padding-bottom: 2pt;
    border-bottom: .6pt solid #9a9a9a;}
.role {font-weight: bold; margin-top: 4pt;}
.when {font-style: italic; color: #555; font-size: %(sm).2fpt;}
ul {margin: 1pt 0 0; padding-left: 16pt; list-style-type: '·';}
li {margin: 0 0 1pt; padding-left: 9pt;}
p {margin: 0;}
.skill {margin: 0 0 1.5pt;}
.skill b {margin-right: 4pt;}
.footer {font-size: 5.6pt; color: #777; margin-top: 8pt;}
"""


def to_html(cv: dict, fs: float = 9.6) -> str:
    e = lambda s: html.escape(str(s))
    ul = lambda items: "<ul>" + "".join(f"<li>{e(b)}</li>" for b in items) + "</ul>" if items else ""
    parts = [f"<h1>{e(cv['name'])}</h1><div class='contact'>{' &nbsp;·&nbsp; '.join(e(c) for c in cv['contact'])}</div>",
             f"<h2>PROFILE</h2><p>{e(cv['profile'])}</p>", "<h2>EXPERIENCE</h2>"]
    for r in cv["experience"]:
        where = " · ".join(e(x) for x in (r.get("dates"), r.get("location")) if x)
        parts.append(f"<div class='role'>{e(r['title'])} — {e(r['company'])}</div><div class='when'>{where}</div>"
                     + ul(r["bullets"]))
    if cv["projects"]:
        parts.append("<h2>PROJECTS</h2>")
        for p in cv["projects"]:
            sub = f" — {e(p['subtitle'])}" if p.get("subtitle") else ""
            parts.append(f"<div class='role'>{e(p['title'])}{sub}</div><div class='when'>{e(p.get('dates', ''))}</div>"
                         + ul(p["bullets"]))
    parts.append("<h2>SKILLS</h2>" + "".join(
        f"<p class='skill'><b>{e(k)}</b> {' · '.join(e(x) for x in v)}</p>" for k, v in cv["skills"]))
    parts.append("<h2>EDUCATION</h2>")
    for d in cv["education"]:
        parts.append(f"<div class='role'>{e(d['degree'])}</div><div class='when'>"
                     f"{' · '.join(e(x) for x in (d.get('school'), d.get('dates')) if x)}</div>"
                     + (f"<p>{' · '.join(e(c) for c in d['coursework'])}</p>" if d.get("coursework") else ""))
    if cv["certificates"]:
        parts.append("<h2>CERTIFICATES</h2>" + ul(cv["certificates"]))
    if cv["footer"]:
        parts.append(f"<div class='footer'>{e(cv['footer'])}</div>")
    style = CSS % {"f": FONTS.as_uri(), "fs": fs, "sm": fs - 0.6, "gap": fs * 0.95}
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head><body>{''.join(parts)}</body></html>"


def fit(cv: dict):
    """One A4 page: shrink the font a little, then drop the least relevant bullets until it fits."""
    from weasyprint import HTML
    cv = {**cv, "experience": [dict(r) for r in cv["experience"]]}
    for fs in (9.6, 9.3, 9.0):
        doc = HTML(string=to_html(cv, fs)).render()
        if len(doc.pages) == 1:
            return doc
    while len(doc.pages) > 1:
        longest = max(cv["experience"], key=lambda r: len(r["bullets"]))
        if len(longest["bullets"]) <= 1:
            break  # can't shrink further - better two pages than lost roles
        longest["bullets"] = longest["bullets"][:-1]  # bullets are ordered most relevant first
        doc = HTML(string=to_html(cv, 9.0)).render()
    return doc


def render_pdf(cv: dict) -> bytes:
    return fit(cv).write_pdf()


def generate(offer: dict, description: str, master: dict, profile: str, cfg: dict, api_key: str, call_gemini):
    """Returns (pdf bytes, answers [{question, answer}]) or (None, problem)."""
    salary = cfg.get("cv_salary") or ""
    availability = cfg.get("cv_availability") or ""
    prompt = PROMPT.format(
        questions="\n".join(f"- {q}" for q in ANSWER_QUESTIONS),
        salary=(f"answer with exactly this expectation: {salary}" if salary else
                "if the offer states a range, say the stated range is acceptable and you are open to discussion; "
                "otherwise write [YOUR EXPECTATION] as a placeholder"),
        availability=(f"answer with exactly: {availability}" if availability else
                      "write [YOUR AVAILABILITY] as a placeholder"),
        master=yaml.safe_dump({k: v for k, v in master.items() if k not in ("contact", "footer")},
                              allow_unicode=True, sort_keys=False),
        profile=profile, title=offer.get("title"), company=offer.get("company"), location=offer.get("location"),
        description=(description or "")[:12000])
    # a stronger model is worth it here (a few CVs a week); the usual list is the fallback
    models = list(dict.fromkeys([*cfg.get("cv_models", []), *cfg.get("gemini_models", [])]))
    t = call_gemini(prompt, SCHEMA, {**cfg, "gemini_models": models}, api_key, temperature=0.3)
    if not t:
        return None, "Gemini is unavailable right now - try again later."
    answers = [a for a in t.get("answers") or [] if a.get("question") and a.get("answer")]
    return render_pdf(merge(master, t)), answers


def file_name(master: dict, offer: dict) -> str:
    slug = lambda s: re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_")[:30]
    return f"CV_{slug(master['name'])}_{slug(offer.get('company')) or 'offer'}.pdf"
