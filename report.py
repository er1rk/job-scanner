"""
Weekly market & skill-gap report.

Everything numeric is computed in Python straight from the database (deterministic,
auditable). The LLM only gets the *aggregated* numbers and turns them into at most
three concrete learning recommendations - it never sees or invents the counts.

Main idea: the most useful gaps are the ones in "near-miss" offers - offers that scored
just below the e-mail threshold. Closing those gaps moves real offers into the "apply" bucket.
"""

from __future__ import annotations

import html
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

MIN_OFFERS_FOR_AI = 10  # below this the sample is too small for recommendations

RECS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING"},
        "recommendations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "skill": {"type": "STRING"},
                    "why": {"type": "STRING"},
                    "first_step": {"type": "STRING"},
                },
                "required": ["skill", "why", "first_step"],
            },
        },
        "market_note": {"type": "STRING"},
    },
    "required": ["headline", "recommendations", "market_note"],
}

RECS_PROMPT = """You are a pragmatic career coach for the candidate below. You get aggregated statistics
about the job offers the candidate's scanner scored in the last week. Using ONLY these numbers:
- headline: one sentence summarising the week.
- recommendations: at most 3 skills/requirements worth working on, ordered by impact. Prefer gaps that
  appear in near-miss offers and that the candidate can realistically close in weeks, not years
  (a short course or a portfolio project beats "get 3 more years of experience"). Do not recommend
  something the profile already covers. For each: why (cite the numbers), first_step (one concrete action
  this week).
- market_note: one sentence on anything notable in categories, work mode or salaries.
Be concrete and brief. Answer in English."""


# ---------------------------------------------------------------- helpers
def _clean(label: str) -> str:
    label = re.sub(r"\s+", " ", str(label)).strip(" .;,-")
    return label


def _top(values: list[str], n: int) -> list[tuple[str, int]]:
    """Case-insensitive counting that displays the most common spelling of each item."""
    counts: Counter = Counter()
    spellings: dict[str, Counter] = defaultdict(Counter)
    for v in values:
        v = _clean(v)
        if not v:
            continue
        key = v.casefold()
        counts[key] += 1
        spellings[key][v] += 1
    return [(spellings[k].most_common(1)[0][0], c) for k, c in counts.most_common(n)]


def _key(label: str) -> str:
    return _clean(label).casefold()


def _rows(store, since: datetime, until: datetime) -> list[dict]:
    q = "SELECT * FROM offers WHERE first_seen >= ? AND first_seen < ?"
    out = []
    for r in store.con.execute(q, (since.isoformat(), until.isoformat())):
        d = dict(r)
        for f in ("strengths", "gaps", "contract_types", "required_tools", "nice_to_have_tools"):
            d[f] = json.loads(d[f]) if d.get(f) else []
        out.append(d)
    return out


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


# ---------------------------------------------------------------- stats
def _health(store, start: datetime, now: datetime) -> dict | None:
    """Reliability and cost of the scanner runs in the period (from the runs table)."""
    cols = {r[1] for r in store.con.execute("PRAGMA table_info(runs)")}
    if "gemini_calls" not in cols:
        return None
    runs = [dict(r) for r in store.con.execute(
        "SELECT * FROM runs WHERE started_at >= ? AND started_at < ?", (start.isoformat(), now.isoformat()))]
    if not runs:
        return None

    def total(k):
        return sum(r.get(k) or 0 for r in runs)

    def minutes(r):
        try:
            return (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds() / 60
        except (TypeError, ValueError):
            return None
    durations = [m for m in (minutes(r) for r in runs) if m is not None]
    portals: dict[str, dict] = defaultdict(lambda: {"cards": 0, "new": 0, "detail_failed": 0, "zero_runs": 0})
    for r in runs:
        for name, st in (json.loads(r["sources"]) if r.get("sources") else {}).items():
            p = portals[name]
            p["cards"] += st.get("cards", 0)
            p["new"] += st.get("new", 0)
            p["detail_failed"] += st.get("detail_failed", 0)
            p["zero_runs"] += int(not st.get("cards"))
    return {
        "runs": len(runs),
        "avg_minutes": round(statistics.mean(durations), 1) if durations else None,
        "max_minutes": round(max(durations), 1) if durations else None,
        "stopped_by_time": sum(bool(r.get("stopped_by_time")) for r in runs),
        "gemini_calls": total("gemini_calls"), "gemini_errors": total("gemini_errors"),
        "tokens_in": total("tokens_in"), "tokens_out": total("tokens_out"),
        "scored": total("scored"), "failed": total("failed"),
        "avg_search_s": round(total("search_seconds") / len(runs)),
        "avg_triage_s": round(total("triage_seconds") / len(runs)),
        "avg_score_s": round(total("score_seconds") / len(runs)),
        "portals": dict(portals),
    }


def compute_stats(store, threshold: int, days: int = 7, now: datetime | None = None,
                  near_miss_band: int = 20) -> dict:
    now = now or datetime.now(timezone.utc)
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    week, prev = _rows(store, start, now), _rows(store, prev_start, start)
    n = len(week)
    near_min = threshold - near_miss_band
    near = [r for r in week if near_min <= (r["score"] or 0) < threshold]

    all_gaps = [g for r in week for g in r["gaps"]]
    prev_gaps = Counter(_key(g) for r in prev for g in r["gaps"])
    gaps = [{"label": lbl, "count": c, "share": _pct(c, n), "prev": prev_gaps.get(_key(lbl), 0)}
            for lbl, c in _top(all_gaps, 10)]

    near_gaps = [{"label": lbl, "count": c, "share": _pct(c, len(near))}
                 for lbl, c in _top([g for r in near for g in r["gaps"]], 8)]

    def as_gap(tool: str) -> int:  # offers that require the tool AND list it as the candidate's gap
        k = _key(tool)
        return sum(k in {_key(t) for t in r["required_tools"]} and k in {_key(g) for g in r["gaps"]}
                   for r in week)
    tools = [{"label": lbl, "count": c, "share": _pct(c, n), "as_gap": as_gap(lbl)}
             for lbl, c in _top([t for r in week for t in r["required_tools"]], 10)]

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in week:
        by_cat[r.get("role_category") or "unknown"].append(r)
    categories = []
    for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lows = [r["salary_min_pln_month"] for r in rs if r.get("salary_min_pln_month")]
        highs = [r["salary_max_pln_month"] for r in rs if r.get("salary_max_pln_month")]
        categories.append({
            "category": cat, "count": len(rs),
            "avg_score": round(statistics.mean(r["score"] or 0 for r in rs)),
            "apply": sum(r["verdict"] == "apply" for r in rs),
            "with_salary": len(lows or highs),
            "median_min": round(statistics.median(lows)) if lows else None,
            "median_max": round(statistics.median(highs)) if highs else None,
        })

    by_src: dict[str, list[dict]] = defaultdict(list)
    for r in week:
        by_src[r.get("source") or "linkedin"].append(r)
    portals = [{"source": src, "count": len(rs), "avg_score": round(statistics.mean(r["score"] or 0 for r in rs)),
                "apply": sum(r["verdict"] == "apply" for r in rs)}
               for src, rs in sorted(by_src.items(), key=lambda kv: -len(kv[1]))]

    triaged = store.triaged_since(start.isoformat()) if hasattr(store, "triaged_since") else []

    health = _health(store, start, now)

    verdicts = Counter(r["verdict"] for r in week)
    return {
        "period_start": start.date().isoformat(), "period_end": now.date().isoformat(),
        "offers": n, "offers_prev_week": len(prev), "offers_total": store.count(),
        "threshold": threshold, "near_miss_range": [near_min, threshold - 1],
        "near_miss_offers": len(near),
        "avg_score": round(statistics.mean(r["score"] or 0 for r in week)) if week else None,
        "verdicts": {v: verdicts.get(v, 0) for v in ("apply", "stretch", "skip")},
        "work_mode": dict(Counter(r.get("work_mode") or "unknown" for r in week).most_common()),
        "english_level": dict(Counter(r.get("english_level") or "unknown" for r in week).most_common()),
        "offers_with_salary_pct": _pct(sum(bool(r.get("salary_min_pln_month") or r.get("salary_max_pln_month"))
                                           for r in week), n),
        "top_gaps": gaps,
        "near_miss_gaps": near_gaps,
        "top_required_tools": tools,
        "categories": categories,
        "sources": portals,
        "triaged_out": len(triaged),
        "health": health,
        "triaged_sample": [f"{t['title']} · {t['company']}" for t in triaged[-12:]],
        "best_offers": [{"title": r["title"], "company": r["company"], "score": r["score"], "url": r["url"]}
                        for r in sorted(week, key=lambda r: -(r["score"] or 0))[:5]],
    }


def ai_recommendations(stats: dict, profile: str, call: Callable[..., dict | None]) -> dict | None:
    """`call` is scanner.call_gemini bound to cfg/api key: call(prompt, schema) -> dict | None."""
    if stats["offers"] < MIN_OFFERS_FOR_AI:
        return None
    data = {k: v for k, v in stats.items() if k not in ("best_offers", "triaged_sample", "health")}  # no URLs/company names needed
    prompt = (f"{RECS_PROMPT}\n\n### Candidate profile\n{profile}\n\n"
              f"### Last week's statistics (JSON)\n{json.dumps(data, ensure_ascii=False, indent=1)}")
    recs = call(prompt, RECS_SCHEMA)
    if recs:
        recs["recommendations"] = recs.get("recommendations", [])[:3]
    return recs


# ---------------------------------------------------------------- rendering
def _fmt_pln(v) -> str:
    return f"{v:,.0f}".replace(",", " ") if v else "–"


def _bar(share: int, colour: str = "#0969da") -> str:
    w = max(2, min(100, share))
    return (f"<div style='background:#eaeef2;width:120px;height:8px;border-radius:4px'>"
            f"<div style='background:{colour};width:{w}%;height:8px;border-radius:4px'></div></div>")


def _trend(now_c: int, prev_c: int) -> str:
    if not prev_c:
        return "<span style='color:#57606a'>new</span>" if now_c else ""
    d = now_c - prev_c
    if d == 0:
        return "<span style='color:#57606a'>=</span>"
    return f"<span style='color:{'#cf222e' if d > 0 else '#1a7f37'}'>{'▲' if d > 0 else '▼'}{abs(d)}</span>"


def build_report_html(stats: dict, recs: dict | None = None, csv_attached: bool = False) -> str:
    e = html.escape
    td = "style='padding:4px 10px 4px 0;vertical-align:middle'"
    th = "style='text-align:left;padding:4px 10px 4px 0;color:#57606a;font-weight:normal;font-size:12px'"
    h2 = "style='font-size:16px;margin:24px 0 8px'"
    out = [f"<div style='font-family:Arial,sans-serif;color:#1f2328;max-width:680px'>",
           f"<h1 style='font-size:20px;margin:0 0 4px'>Weekly market &amp; skill-gap report</h1>",
           f"<div style='color:#57606a'>{e(stats['period_start'])} – {e(stats['period_end'])}</div>"]

    v = stats["verdicts"]
    diff = stats["offers"] - stats["offers_prev_week"]
    out.append(
        "<table style='margin:16px 0;border-collapse:collapse'><tr>" + "".join(
            f"<td style='padding:8px 16px 8px 0'><div style='font-size:24px;font-weight:bold'>{val}</div>"
            f"<div style='color:#57606a;font-size:12px'>{lbl}</div></td>"
            for val, lbl in [
                (stats["offers"], f"offers scored ({'+' if diff >= 0 else ''}{diff} vs prev. week)"),
                (stats["avg_score"] if stats["avg_score"] is not None else "–", "average score"),
                (v["apply"], "apply"), (v["stretch"], "stretch"),
                (stats["near_miss_offers"],
                 f"near misses ({stats['near_miss_range'][0]}–{stats['near_miss_range'][1]})"),
            ]) + "</tr></table>")

    if stats["offers"] == 0:
        out.append("<p>No offers were scored this week.</p></div>")
        return "".join(out)

    if recs:
        out.append(f"<div style='background:#ddf4ff;padding:12px 16px;border-radius:8px'>"
                   f"<b>{e(recs.get('headline', ''))}</b><ol style='margin:8px 0 0;padding-left:20px'>")
        for r in recs["recommendations"]:
            out.append(f"<li style='margin-bottom:6px'><b>{e(r['skill'])}</b> – {e(r['why'])}<br>"
                       f"<span style='color:#57606a'>First step: {e(r['first_step'])}</span></li>")
        out.append("</ol>")
        if recs.get("market_note"):
            out.append(f"<div style='margin-top:6px;color:#57606a'>{e(recs['market_note'])}</div>")
        out.append("</div>")
    elif stats["offers"] < MIN_OFFERS_FOR_AI:
        out.append(f"<p style='color:#57606a'>Only {stats['offers']} offers this week – recommendations start "
                   f"from {MIN_OFFERS_FOR_AI}.</p>")

    if stats["near_miss_gaps"]:
        out.append(f"<h2 {h2}>What blocks near-miss offers</h2>"
                   f"<div style='color:#57606a;font-size:12px;margin-bottom:6px'>Gaps in the "
                   f"{stats['near_miss_offers']} offers that scored just below the e-mail threshold.</div>"
                   f"<table style='border-collapse:collapse'>")
        for g in stats["near_miss_gaps"]:
            out.append(f"<tr><td {td}>{e(g['label'])}</td><td {td}>{_bar(g['share'], '#bf8700')}</td>"
                       f"<td {td}>{g['count']} ({g['share']}%)</td></tr>")
        out.append("</table>")

    out.append(f"<h2 {h2}>Most common gaps (all offers)</h2><table style='border-collapse:collapse'>"
               f"<tr><th {th}>gap</th><th {th}></th><th {th}>offers</th><th {th}>vs prev.</th></tr>")
    for g in stats["top_gaps"]:
        out.append(f"<tr><td {td}>{e(g['label'])}</td><td {td}>{_bar(g['share'], '#cf222e')}</td>"
                   f"<td {td}>{g['count']} ({g['share']}%)</td><td {td}>{_trend(g['count'], g['prev'])}</td></tr>")
    out.append("</table>")

    out.append(f"<h2 {h2}>Most required tools</h2><table style='border-collapse:collapse'>"
               f"<tr><th {th}>tool</th><th {th}></th><th {th}>offers</th><th {th}>of which your gap</th></tr>")
    for t in stats["top_required_tools"]:
        out.append(f"<tr><td {td}>{e(t['label'])}</td><td {td}>{_bar(t['share'])}</td>"
                   f"<td {td}>{t['count']} ({t['share']}%)</td><td {td}>{t['as_gap'] or ''}</td></tr>")
    out.append("</table>")

    out.append(f"<h2 {h2}>Categories</h2><table style='border-collapse:collapse'>"
               f"<tr><th {th}>category</th><th {th}>offers</th><th {th}>avg score</th><th {th}>apply</th>"
               f"<th {th}>median salary PLN/month*</th></tr>")
    for c in stats["categories"]:
        sal = (f"{_fmt_pln(c['median_min'])} – {_fmt_pln(c['median_max'])} <span style='color:#57606a'>"
               f"(n={c['with_salary']})</span>") if c["with_salary"] else "–"
        out.append(f"<tr><td {td}>{e(c['category'])}</td><td {td}>{c['count']}</td><td {td}>{c['avg_score']}</td>"
                   f"<td {td}>{c['apply']}</td><td {td}>{sal}</td></tr>")
    out.append("</table>")
    wm = " · ".join(f"{e(k)} {_pct(c, stats['offers'])}%" for k, c in stats["work_mode"].items())
    out.append(f"<div style='color:#57606a;font-size:12px;margin-top:6px'>* only {stats['offers_with_salary_pct']}% "
               f"of offers state a salary; gross and B2B rates are mixed. Work mode: {wm}.</div>")

    if len(stats.get("sources") or []) > 1:
        labels = {"linkedin": "LinkedIn", "pracuj": "pracuj.pl", "nofluffjobs": "No Fluff Jobs", "justjoin": "justjoin.it", "rocketjobs": "RocketJobs"}
        out.append(f"<h2 {h2}>Portals</h2><table style='border-collapse:collapse'>"
                   f"<tr><th {th}>portal</th><th {th}>offers</th><th {th}>avg score</th><th {th}>apply</th></tr>")
        for src in stats["sources"]:
            out.append(f"<tr><td {td}>{e(labels.get(src['source'], src['source']))}</td><td {td}>{src['count']}</td>"
                       f"<td {td}>{src['avg_score']}</td><td {td}>{src['apply']}</td></tr>")
        out.append("</table>")

    out.append(f"<h2 {h2}>Best offers this week</h2><ol style='padding-left:20px;margin:0'>")
    for o in stats["best_offers"]:
        out.append(f"<li><b>{o['score']}</b> – <a href='{e(o['url'] or '')}'>{e(o['title'] or '')}</a>"
                   f" · {e(o['company'] or '')}</li>")
    out.append("</ol>")

    if stats.get("triaged_out"):
        out.append(f"<h2 {h2}>Skipped by the AI pre-filter</h2><div style='color:#57606a;font-size:12px'>"
                   f"{stats['triaged_out']} offers were dropped this week on title alone. Latest ones – check that "
                   f"nothing relevant is being filtered out:</div><ul style='margin:6px 0;padding-left:20px;"
                   f"color:#57606a;font-size:13px'>")
        out += [f"<li>{e(x)}</li>" for x in stats["triaged_sample"]]
        out.append("</ul>")

    h = stats.get("health")
    if h:
        out.append(f"<h2 {h2}>System health</h2><table style='border-collapse:collapse;font-size:13px'>")
        rows = [
            ("runs", f"{h['runs']} (avg {h['avg_minutes']} min, longest {h['max_minutes']} min)"),
            ("stopped by time budget", f"{h['stopped_by_time']} run(s)"),
            ("avg stage time", f"search {h['avg_search_s']}s · pre-filter {h['avg_triage_s']}s · scoring {h['avg_score_s']}s"),
            ("Gemini", f"{h['gemini_calls']} calls, {h['gemini_errors']} failed · "
                       f"{h['tokens_in']:,} tokens in / {h['tokens_out']:,} out".replace(",", " ")),
            ("offers scored / failed", f"{h['scored']} / {h['failed']}"),
        ]
        out += [f"<tr><td {td}><span style='color:#57606a'>{k}</span></td><td {td}>{e(v)}</td></tr>" for k, v in rows]
        out.append("</table>")
        if h["portals"]:
            out.append(f"<table style='border-collapse:collapse;font-size:13px;margin-top:8px'><tr><th {th}>portal</th>"
                       f"<th {th}>found</th><th {th}>new</th><th {th}>description errors</th><th {th}>runs with 0 found</th></tr>")
            for name, p in sorted(h["portals"].items(), key=lambda kv: -kv[1]["cards"]):
                warn = " style='color:#cf222e;font-weight:bold'" if p["zero_runs"] else ""
                out.append(f"<tr><td {td}>{e(name)}</td><td {td}>{p['cards']}</td><td {td}>{p['new']}</td>"
                           f"<td {td}>{p['detail_failed']}</td><td {td}><span{warn}>{p['zero_runs']}</span></td></tr>")
            out.append("</table>")

    foot = f"{stats['offers_total']} offers in the database."
    if csv_attached:
        foot += " Full dataset attached as jobs.csv."
    out.append(f"<p style='color:#57606a;font-size:12px;margin-top:24px'>{foot}</p></div>")
    return "".join(out)


def report_text(stats: dict, recs: dict | None = None) -> str:
    """Short plain-text version for local use (python scanner.py --report).
    Not printed in GitHub Actions - gap names would end up in public logs."""
    lines = [f"Week {stats['period_start']} – {stats['period_end']}: {stats['offers']} offers, "
             f"avg score {stats['avg_score']}, near misses {stats['near_miss_offers']}"]
    lines += [f"  gap  {g['count']:>3}  {g['label']}" for g in stats["top_gaps"][:5]]
    if recs:
        lines += [f"  rec  {r['skill']}" for r in recs["recommendations"]]
    return "\n".join(lines)
