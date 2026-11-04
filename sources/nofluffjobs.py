"""No Fluff Jobs - public JSON API used by the website."""

from __future__ import annotations

import time

from . import common

NAME = "nofluffjobs"
LABEL = "No Fluff Jobs"
SEARCH_URL = ("https://nofluffjobs.com/api/search/posting?page={page}&salaryCurrency=PLN"
              "&salaryPeriod=month&region=pl&language=pl-PL")
DETAIL_URL = "https://nofluffjobs.com/api/posting/{id}"
VIEW_URL = "https://nofluffjobs.com/pl/job/{url}"


def parse_search(data: dict) -> list[dict]:
    out = []
    for p in (data or {}).get("postings") or []:
        if not p.get("id"):
            continue
        places = (p.get("location") or {}).get("places") or []
        cities = [pl.get("city") for pl in places if pl.get("city")]
        sal = p.get("salary") or {}
        salary = (f"{sal.get('from')}-{sal.get('to')} {sal.get('currency', '')} ({sal.get('type', '')})"
                  if sal.get("from") else "")
        out.append({
            "id": f"nfj:{p['id']}",
            "source": NAME,
            "title": p.get("title", ""),
            "company": p.get("name", ""),
            "location": ", ".join(dict.fromkeys(cities[:4])) + (" (remote)" if p.get("fullyRemote") else ""),
            "url": VIEW_URL.format(url=p.get("url") or p["id"]),
            # offers are mostly "renewed" (bumped) rather than newly posted - use the later of the two
            "posted": common.utc_from_ms(max(p.get("posted") or 0, p.get("renewed") or 0)).isoformat()
            if (p.get("posted") or p.get("renewed")) else None,
            "extra": " | ".join(x for x in (salary, ", ".join(p.get("seniority") or [])) if x),
        })
    return out


def parse_detail(d: dict) -> str:
    if not d:
        return ""
    req, specs = d.get("requirements") or {}, d.get("specs") or {}
    vals = lambda items: ", ".join(i.get("value", "") for i in items or [] if i.get("value"))
    salary = ((d.get("essentials") or {}).get("originalSalary") or {}).get("types") or {}
    salary_txt = "; ".join(f"{k}: {v.get('range')} per {v.get('period')}" for k, v in salary.items() if isinstance(v, dict))
    langs = ", ".join(f"{l.get('code')} ({l.get('type')})" for l in req.get("languages") or [])
    parts = [
        f"Seniority: {', '.join((d.get('basics') or {}).get('seniority') or [])}",
        f"Salary: {salary_txt}" if salary_txt else "",
        f"Must have: {vals(req.get('musts'))}" if req.get("musts") else "",
        f"Nice to have: {vals(req.get('nices'))}" if req.get("nices") else "",
        f"Languages: {langs}" if langs else "",
        "Daily tasks:\n- " + "\n- ".join(specs.get("dailyTasks")) if specs.get("dailyTasks") else "",
        common.html_to_text(req.get("description")),
        common.html_to_text((d.get("details") or {}).get("description")),
    ]
    return "\n\n".join(p for p in parts if p).strip()


def search(searches: list[dict], cfg: dict, hours: int) -> list[dict]:
    out = []
    for s in searches:
        kw = s["keywords"].replace("'", " ")
        city = "remote" if s.get("remote") else common.slug(common.polish_city(s["location"]))
        for page in range(1, int(cfg.get("pages_per_search", 1)) + 1):
            data = common.post_json(SEARCH_URL.format(page=page), {"rawSearch": f"keyword='{kw}' city={city}"},
                                    content_type="application/infiniteSearch+json")
            if not data:
                break
            cards = parse_search(data)
            for o in cards:
                o["search_keyword"] = s["keywords"]
            out += [o for o in cards if common.is_recent(common.utc_from_iso(o.get("posted")), hours)]
            time.sleep(1)
            if page >= int(data.get("totalPages") or 1):
                break
    return out


def detail(offer: dict) -> str | None:
    d = common.get_json(DETAIL_URL.format(id=offer["id"].split(":", 1)[1]))
    if d is None:
        return None
    text = parse_detail(d)
    return f"{offer['extra']}\n\n{text}".strip() if text and offer.get("extra") else text
