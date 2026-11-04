"""justjoin.it - candidate API (newest first) for the list, JobPosting JSON-LD on the offer page."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from . import common

NAME = "justjoin"
LABEL = "justjoin.it"
PAGE_SIZE = 100
SEARCH_URL = ("https://justjoin.it/api/candidate-api/offers?from={start}&itemsCount={n}"
              "&sortBy=publishedAt&orderBy=descending&keywords={kw}")
VIEW_URL = "https://justjoin.it/job-offer/{slug}"


def _cities(o: dict) -> list[str]:
    return [c for c in [o.get("city")] + [l.get("city") for l in o.get("locations") or []] if c]


def parse_search(data: dict) -> list[dict]:
    out = []
    for o in (data or {}).get("data") or []:
        if not o.get("slug"):
            continue
        salaries = []
        for e in o.get("employmentTypes") or []:
            if e.get("from") and e.get("currencySource") != "conversion":
                salaries.append(f"{e.get('from')}-{e.get('to')} {(e.get('currency') or '').upper()} "
                                f"/{e.get('unit')} {e.get('type')} {'gross' if e.get('gross') else 'net'}")
        skills = ", ".join(s.get("name", "") for s in o.get("requiredSkills") or [] if isinstance(s, dict))
        nice = ", ".join(s.get("name", "") for s in o.get("niceToHaveSkills") or [] if isinstance(s, dict))
        facts = [f"Experience level: {o.get('experienceLevel')}", f"Work mode: {o.get('workplaceType')}",
                 "Salary: " + "; ".join(salaries) if salaries else "",
                 f"Required skills: {skills}" if skills else "", f"Nice to have: {nice}" if nice else ""]
        out.append({
            "id": f"jj:{o['slug']}",
            "source": NAME,
            "title": o.get("title", ""),
            "company": o.get("companyName", ""),
            "location": ", ".join(dict.fromkeys(_cities(o)[:3])) + f" ({o.get('workplaceType')})",
            "url": VIEW_URL.format(slug=o["slug"]),
            "posted": o.get("publishedAt"),
            "extra": " | ".join(f for f in facts if f),
            "_cities": _cities(o),
            "_remote": o.get("workplaceType") == "remote",
        })
    return out


def parse_detail(page: str) -> str:
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return common.html_to_text(d.get("description"))
    return ""


def wanted(o: dict, cities: set[str], remote_ok: bool) -> bool:
    """justjoin.it also lists jobs abroad - keep configured cities, and remote jobs based in Poland."""
    keys = {common.city_key(c) for c in o["_cities"]}
    if keys & cities:
        return True
    return remote_ok and o["_remote"] and bool(keys & common.POLISH_CITIES)


def search(searches: list[dict], cfg: dict, hours: int) -> list[dict]:
    """One query per keyword (the API has no reliable city filter) - locations are filtered here."""
    cities = {common.city_key(s["location"]) for s in searches if not s.get("remote")}
    remote_ok = any(s.get("remote") for s in searches)
    out = []
    for kw in dict.fromkeys(s["keywords"] for s in searches):
        for page in range(max(1, int(cfg.get("pages_per_search", 1)))):
            data = common.get_json(SEARCH_URL.format(start=page * PAGE_SIZE, n=PAGE_SIZE, kw=quote(kw)),
                                   headers={"Version": "2"})
            if not data:
                break
            cards = parse_search(data)
            recent = [o for o in cards if common.is_recent(common.utc_from_iso(o.get("posted")), hours)]
            for o in recent:
                o["search_keyword"] = kw
            out += [o for o in recent if wanted(o, cities, remote_ok)]
            time.sleep(1)
            if len(recent) < len(cards) or len(cards) < PAGE_SIZE:  # sorted newest first - older ones follow
                break
    for o in out:
        o.pop("_cities", None)
        o.pop("_remote", None)
    return out


def detail(offer: dict) -> str | None:
    body = common.get(offer["url"])
    if body is None:
        return None
    text = parse_detail(body)
    return f"{offer['extra']}\n\n{text}".strip() if text and offer.get("extra") else text
