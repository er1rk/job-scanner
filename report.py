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


ALIASES = {"powerbi": "power bi", "ms power bi": "power bi", "excel ms": "excel", "advanced excel": "excel",
           "sql server": "sql server", "ms sql": "sql server", "t-sql": "sql", "mysql": "mysql"}


def _key(label: str) -> str:
    """Comparable form: case-insensitive, 'MS Excel' / 'Microsoft Excel' -> 'excel'."""
    k = _clean(label).casefold()
    k = re.sub(r"^(ms|microsoft)\s+(?=\S{3,})", "", k)
    return ALIASES.get(k, k)


def _top(values: list[str], n: int) -> list[tuple[str, int]]:
    """Case-insensitive counting that displays the most common spelling of each item."""
    counts: Counter = Counter()
    spellings: dict[str, Counter] = defaultdict(Counter)
    for v in values:
        v = _clean(v)
        if not v:
            continue
        key = _key(v)
        counts[key] += 1
        spellings[key][v] += 1
    def display(k):  # most common spelling, but "Excel" rather than "MS Excel"
        return sorted(spellings[k].items(), key=lambda kv: (bool(re.match(r"(?i)(ms|microsoft) ", kv[0])), -kv[1],
                                                            len(kv[0])))[0][0]
    return [(display(k), c) for k, c in counts.most_common(n)]


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
        both = [r for r in rs if r.get("salary_min_pln_month") and r.get("salary_max_pln_month")]
        lows = [r["salary_min_pln_month"] for r in both]
        highs = [r["salary_max_pln_month"] for r in both]
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
        "triaged_sample": [f"{t['title']} · {t['company']}" for t in triaged[-8:]],
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
# E-mail-safe HTML: table layout + inline styles (Gmail/Outlook ignore most <style> rules and flexbox).
INK, INK2, MUTED = "#1f1f1e", "#52514e", "#8a8984"
PAGE, CARD, LINE, TRACK = "#f4f3f0", "#ffffff", "#e7e5e0", "#eceae6"
ACCENT = "#2a78d6"            # one hue for every magnitude bar - bars encode size, not identity
GOOD, WARN = "#1a7f37", "#b42318"
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
NUM = "font-variant-numeric:tabular-nums;white-space:nowrap"
PORTALS = {"linkedin": "LinkedIn", "pracuj": "pracuj.pl", "nofluffjobs": "No Fluff Jobs",
           "justjoin": "justjoin.it", "rocketjobs": "RocketJobs"}


def _n(v) -> str:
    """1234567 -> '1 234 567' (Polish-style thin grouping that no client turns into a link)."""
    return f"{v:,.0f}".replace(",", " ") if isinstance(v, (int, float)) else "–"


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _card(title: str, body: str, note: str = "") -> str:
    note_html = (f"<div style='font-size:12px;line-height:17px;color:{MUTED};margin:2px 0 12px'>{note}</div>"
                 if note else "<div style='height:10px'></div>")
    return (f"<tr><td style='padding:0 0 12px'><table role='presentation' width='100%' cellpadding='0' "
            f"cellspacing='0' style='background:{CARD};border:1px solid {LINE};border-radius:10px'><tr>"
            f"<td style='padding:18px 20px'><div style='font-size:15px;line-height:20px;font-weight:600;"
            f"color:{INK}'>{title}</div>{note_html}{body}</td></tr></table></td></tr>")


def _bars(items: list[dict], value: Callable[[dict], str], extra: Callable[[dict], str] | None = None,
          extra_w: int = 44) -> str:
    """Label | bar (share of offers) | value [| extra] rows."""
    if not items:
        return f"<div style='font-size:13px;color:{MUTED}'>No data this week.</div>"
    top = max(i["share"] for i in items) or 1
    rows = []
    for it in items:
        w = max(2, round(100 * it["share"] / top))  # scale to the largest bar so short lists stay readable
        bar = (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'><tr>"
               f"<td style='background:{TRACK};border-radius:4px;height:8px;line-height:8px;font-size:0'>"
               f"<table role='presentation' width='{w}%' cellpadding='0' cellspacing='0'><tr>"
               f"<td style='background:{ACCENT};border-radius:4px;height:8px;line-height:8px;font-size:0'>&nbsp;"
               f"</td></tr></table></td></tr></table>")
        ex = (f"<td width='{extra_w}' style='padding:5px 0 5px 10px;font-size:12px;color:{MUTED};text-align:right;"
              f"width:{extra_w}px;{NUM}'>{extra(it)}</td>") if extra else ""
        rows.append(f"<tr><td width='34%' style='padding:5px 12px 5px 0;font-size:13px;color:{INK};width:34%'>"
                    f"{_esc(it['label'])}</td>"
                    f"<td style='padding:5px 0;min-width:60px'>{bar}</td>"
                    f"<td width='70' style='padding:5px 0 5px 12px;font-size:13px;color:{INK2};text-align:right;width:70px;{NUM}'>"
                    f"{value(it)}</td>{ex}</tr>")
    return (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='table-layout:fixed'>"
            f"{''.join(rows)}</table>")


def _table(head: list[str], rows: list[list[str]], align: list[str]) -> str:
    th = "".join(f"<td style='padding:0 0 6px;font-size:11px;letter-spacing:.02em;text-transform:uppercase;"
                 f"color:{MUTED};text-align:{a};{'padding-left:10px;' if k else ''}'>{h}</td>"
                 for k, (h, a) in enumerate(zip(head, align)))
    body = "".join(
        "<tr>" + "".join(f"<td style='padding:7px 0;border-top:1px solid {LINE};font-size:13px;color:{INK};"
                         f"text-align:{a};{'font-variant-numeric:tabular-nums;' if a == 'right' else ''}"
                         f"{'padding-left:10px;' if k else ''}'>{c}</td>"
                         for k, (c, a) in enumerate(zip(r, align))) + "</tr>"
        for r in rows)
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'><tr>{th}</tr>{body}</table>"


def _kpis(stats: dict) -> str:
    v, prev = stats["verdicts"], stats["offers_prev_week"]
    diff = stats["offers"] - prev
    delta = (f"<div style='font-size:12px;color:{MUTED};{NUM}'>{'+' if diff >= 0 else '−'}{_n(abs(diff))} vs prev. week</div>"
             if prev else f"<div style='font-size:12px;color:{MUTED}'>first week</div>")
    lo, hi = stats["near_miss_range"]
    tiles = [("Offers scored", _n(stats["offers"]), delta),
             ("Average score", _n(stats["avg_score"]) if stats["avg_score"] is not None else "–",
              f"<div style='font-size:12px;color:{MUTED}'>out of 100</div>"),
             ("Apply", _n(v["apply"]), f"<div style='font-size:12px;color:{MUTED}'>{_n(v['stretch'])} stretch</div>"),
             ("Near misses", _n(stats["near_miss_offers"]),
              f"<div style='font-size:12px;color:{MUTED}'>score {lo}–{hi}</div>")]
    def tile(k, label, value, sub):
        return (
        f"<td width='50%' style='padding:0 {0 if k % 2 else 8}px {0 if k >= 2 else 8}px 0;vertical-align:top'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{CARD};"
        f"border:1px solid {LINE};border-radius:10px'><tr><td style='padding:14px 14px 12px'>"
        f"<div style='font-size:12px;color:{INK2}'>{label}</div>"
        f"<div style='font-size:26px;line-height:32px;font-weight:600;color:{INK};{NUM}'>{value}</div>{sub}"
        f"</td></tr></table></td>")
    # 2 x 2 grid: fits a phone screen without media queries (which many mail apps ignore)
    cells = [tile(k, *t) for k, t in enumerate(tiles)]
    return (f"<tr><td style='padding:0 0 12px'><table role='presentation' width='100%' cellpadding='0' "
            f"cellspacing='0'><tr>{cells[0]}{cells[1]}</tr><tr>{cells[2]}{cells[3]}</tr></table></td></tr>")


def _recs(recs: dict) -> str:
    items = "".join(
        f"<tr><td style='vertical-align:top;padding:8px 10px 8px 0;width:22px'>"
        f"<div style='width:22px;height:22px;line-height:22px;border-radius:11px;background:{ACCENT};color:#fff;"
        f"font-size:12px;font-weight:600;text-align:center'>{k}</div></td>"
        f"<td style='padding:8px 0;font-size:13px;line-height:19px;color:{INK}'><b>{_esc(r['skill'])}</b> – "
        f"{_esc(r['why'])}<div style='color:{INK2};margin-top:2px'>First step: {_esc(r['first_step'])}</div></td></tr>"
        for k, r in enumerate(recs["recommendations"], 1))
    note = f"<div style='font-size:12px;color:{MUTED};margin-top:6px'>{_esc(recs['market_note'])}</div>" \
        if recs.get("market_note") else ""
    return _card(_esc(recs.get("headline") or "This week's recommendations"),
                 f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{items}</table>{note}")


def _health_card(h: dict) -> str:
    kv = [("Runs", f"{h['runs']} · avg {h['avg_minutes']} min · longest {h['max_minutes']} min"),
          ("Stopped by time budget", f"{h['stopped_by_time']} run(s)"),
          ("Avg stage time", f"search {h['avg_search_s']} s · pre-filter {h['avg_triage_s']} s · "
                             f"scoring {h['avg_score_s']} s"),
          ("Gemini", f"{_n(h['gemini_calls'])} calls · {_n(h['gemini_errors'])} failed · "
                     f"{_n(h['tokens_in'])} tokens in / {_n(h['tokens_out'])} out"),
          ("Offers scored / failed", f"{_n(h['scored'])} / {_n(h['failed'])}")]
    rows = "".join(f"<tr><td style='padding:3px 12px 3px 0;font-size:12px;color:{MUTED};white-space:nowrap;"
                   f"vertical-align:top'>{k}</td><td style='padding:3px 0;font-size:12px;color:{INK2}'>{_esc(v)}</td></tr>"
                   for k, v in kv)
    portal_rows = []
    for name, p in sorted(h["portals"].items(), key=lambda kv: -kv[1]["cards"]):
        zero = (f"<span style='color:{WARN};font-weight:600'>⚠ {p['zero_runs']}</span>" if p["zero_runs"] else "0")
        portal_rows.append([_esc(PORTALS.get(name, name)), _n(p["cards"]), _n(p["new"]), _n(p["detail_failed"]), zero])
    ptable = _table(["Portal", "Found", "New", "Description errors", "Runs with 0 found"], portal_rows,
                    ["left", "right", "right", "right", "right"]) if portal_rows else ""
    return _card("System health",
                 f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin-bottom:12px'>{rows}</table>"
                 f"{ptable}")


def build_report_html(stats: dict, recs: dict | None = None, csv_attached: bool = False) -> str:
    n = stats["offers"]
    blocks = [_kpis(stats)]
    if n == 0:
        blocks.append(_card("No offers this week", f"<div style='font-size:13px;color:{INK2}'>"
                                                   f"The scanner didn't score any offers in this period.</div>"))
    else:
        if recs and recs.get("recommendations"):
            blocks.append(_recs(recs))
        elif n < MIN_OFFERS_FOR_AI:
            blocks.append(_card("Recommendations", f"<div style='font-size:13px;color:{INK2}'>Only {n} offers this "
                                                   f"week – recommendations start from {MIN_OFFERS_FOR_AI}.</div>"))
        lo, hi = stats["near_miss_range"]
        if stats["near_miss_gaps"]:
            blocks.append(_card(
                "What blocks near-miss offers",
                _bars(stats["near_miss_gaps"], lambda g: f"{_n(g['count'])} · {g['share']}%"),
                f"Gaps in the {_n(stats['near_miss_offers'])} offers that scored {lo}–{hi}, just below the e-mail "
                f"threshold. Closing these turns real offers into “apply”."))
        show_trend = stats["offers_prev_week"] > 0

        def trend(g):
            d = g["count"] - g["prev"]
            if d == 0:
                return "="
            return f"<span style='color:{WARN if d > 0 else GOOD}'>{'▲' if d > 0 else '▼'} {abs(d)}</span>"
        blocks.append(_card("Most common gaps", _bars(stats["top_gaps"], lambda g: f"{_n(g['count'])} · {g['share']}%",
                                                      trend if show_trend else None),
                            f"Share of all {_n(n)} offers" + (" · change vs previous week" if show_trend else "")))
        blocks.append(_card("Most required tools",
                            _bars(stats["top_required_tools"], lambda t: f"{_n(t['count'])} · {t['share']}%",
                                  lambda t: f"gap {_n(t['as_gap'])}" if t["as_gap"] else "", extra_w=56),
                            "Share of offers that require the tool · “gap” = offers where it was your gap"))
        cat_rows = []
        for c in stats["categories"]:
            sal = (f"<div style='font-size:12px;color:{MUTED};margin-top:1px'>salary "
                   f"<span style='white-space:nowrap'>{_n(c['median_min'])}–{_n(c['median_max'])} PLN</span> "
                   f"<span style='white-space:nowrap'>· {c['with_salary']} offers</span></div>") if c["with_salary"] else ""
            apply = f"<b style='color:{GOOD}'>{_n(c['apply'])}</b>" if c["apply"] else "0"
            cat_rows.append([f"{_esc(c['category'])}{sal}", _n(c["count"]), _n(c["avg_score"]), apply])
        wm = " · ".join(f"{_esc(k)} {_pct(c, n)}%" for k, c in stats["work_mode"].items())
        blocks.append(_card("Categories", _table(["Category", "Offers", "Avg", "Apply"], cat_rows,
                                                 ["left", "right", "right", "right"]),
                            f"Salary = median monthly range of offers stating both ends; only "
                            f"{stats['offers_with_salary_pct']}% of offers state a salary, gross and B2B mixed. "
                            f"Work mode: {wm}."))
        if len(stats.get("sources") or []) > 1:
            rows = [[_esc(PORTALS.get(s_['source'], s_['source'])), _n(s_["count"]), _n(s_["avg_score"]),
                     f"<b style='color:{GOOD}'>{_n(s_['apply'])}</b>" if s_["apply"] else "0"]
                    for s_ in stats["sources"]]
            blocks.append(_card("Portals", _table(["Portal", "Offers", "Avg score", "Apply"], rows,
                                                  ["left", "right", "right", "right"])))
        best = "".join(
            f"<tr><td style='padding:9px 12px 9px 0;width:40px;vertical-align:top;"
            f"border-top:{'0' if k == 0 else '1px solid ' + LINE}'>"
            f"<div style='font-size:13px;font-weight:600;color:#fff;background:{GOOD if o['score'] >= stats['threshold'] else MUTED};"
            f"border-radius:6px;text-align:center;padding:3px 0;{NUM}'>{o['score']}</div></td>"
            f"<td style='padding:9px 0;border-top:{'0' if k == 0 else '1px solid ' + LINE};font-size:13px;line-height:18px'>"
            f"<a href='{_esc(o['url'])}' style='color:{INK};font-weight:600;text-decoration:none'>{_esc(o['title'])}</a>"
            f"<div style='color:{INK2}'>{_esc(o['company'])}</div></td></tr>"
            for k, o in enumerate(stats["best_offers"]))
        blocks.append(_card("Best offers this week",
                            f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{best}</table>"))
        if stats.get("triaged_out"):
            items = "".join(f"<li style='margin:0 0 3px'>{_esc(x)}</li>" for x in stats["triaged_sample"])
            blocks.append(_card(f"Skipped by the AI pre-filter · {_n(stats['triaged_out'])}",
                                f"<ul style='margin:0;padding-left:18px;font-size:12px;line-height:17px;color:{INK2}'>"
                                f"{items}</ul>",
                                "Dropped on title alone. Latest ones – check that nothing relevant is filtered out."))
    if stats.get("health"):
        blocks.append(_health_card(stats["health"]))

    foot = f"{_n(stats['offers_total'])} offers in the database"
    if csv_attached:
        foot += " · full dataset attached as jobs.csv"
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<meta name='color-scheme' content='light only'><meta name='supported-color-schemes' content='light'></head>"
        f"<body style='margin:0;padding:0;background:{PAGE}'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{PAGE}'><tr>"
        f"<td align='center' style='padding:24px 12px'>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:640px;"
        f"font-family:{FONT};color:{INK}'>"
        f"<tr><td style='padding:0 2px 16px'><div style='font-size:12px;letter-spacing:.04em;text-transform:uppercase;"
        f"color:{MUTED}'>Job scanner · weekly report</div>"
        f"<div style='font-size:22px;line-height:28px;font-weight:600;color:{INK};margin-top:2px'>"
        f"Market &amp; skill-gap report</div>"
        f"<div style='font-size:13px;color:{INK2};margin-top:2px'>{_esc(stats['period_start'])} – "
        f"{_esc(stats['period_end'])}</div></td></tr>"
        + "".join(blocks) +
        f"<tr><td style='padding:4px 2px 0;font-size:12px;color:{MUTED};text-align:center'>{foot}</td></tr>"
        f"</table></td></tr></table></body></html>")


def report_text(stats: dict, recs: dict | None = None) -> str:
    """Short plain-text version for local use (python scanner.py --report).
    Not printed in GitHub Actions - gap names would end up in public logs."""
    lines = [f"Week {stats['period_start']} – {stats['period_end']}: {stats['offers']} offers, "
             f"avg score {stats['avg_score']}, near misses {stats['near_miss_offers']}"]
    lines += [f"  gap  {g['count']:>3}  {g['label']}" for g in stats["top_gaps"][:5]]
    if recs:
        lines += [f"  rec  {r['skill']}" for r in recs["recommendations"]]
    return "\n".join(lines)
