"""justjoin.it - candidate API (newest first) for the list, JobPosting JSON-LD on the offer page."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from . import common

PAGE_SIZE = 100


def _cities(o: dict) -> list[str]:
    return [c for c in [o.get("city")] + [l.get("city") for l in o.get("locations") or []] if c]


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
    """These portals also list jobs abroad - keep configured cities, and remote jobs based in Poland."""
    keys = {common.city_key(c) for c in o["_cities"]}
    if keys & cities:
        return True
    return remote_ok and o["_remote"] and bool(keys & common.POLISH_CITIES)


class Platform:
    """justjoin.it and its sister site rocketjobs.pl (non-IT jobs) run on the same API and page format."""

    def __init__(self, name: str, label: str, prefix: str, api: str, view: str):
        self.name, self.label, self.prefix = name, label, prefix
        self.search_url = api + "?from={start}&itemsCount={n}&sortBy=publishedAt&orderBy=descending&keywords={kw}"
        self.view_url = view

    def parse_search(self, data: dict) -> list[dict]:
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
                "id": f"{self.prefix}:{o['slug']}",
                "source": self.name,
                "title": o.get("title", ""),
                "company": o.get("companyName", ""),
                "location": ", ".join(dict.fromkeys(_cities(o)[:3])) + f" ({o.get('workplaceType')})",
                "url": self.view_url.format(slug=o["slug"]),
                "posted": o.get("publishedAt"),
                "extra": " | ".join(f for f in facts if f),
                "_cities": _cities(o),
                "_remote": o.get("workplaceType") == "remote",
            })
        return out

    def search(self, searches: list[dict], cfg: dict, hours: int) -> list[dict]:
        """One query per keyword (the API has no reliable city filter) - locations are filtered here."""
        cities = {common.city_key(s["location"]) for s in searches if not s.get("remote")}
        remote_ok = any(s.get("remote") for s in searches)
        out = []
        for kw in dict.fromkeys(s["keywords"] for s in searches):
            for page in range(max(1, int(cfg.get("pages_per_search", 1)))):
                data = common.get_json(self.search_url.format(start=page * PAGE_SIZE, n=PAGE_SIZE, kw=quote(kw)),
                                       headers={"Version": "2"})
                if not data:
                    break
                cards = self.parse_search(data)
                recent = [o for o in cards if common.is_recent(common.utc_from_iso(o.get("posted")), hours)]
                for o in recent:
                    o["search_keyword"] = kw
                out += [o for o in recent if wanted(o, cities, remote_ok)]
                time.sleep(1)
                if len(recent) < len(cards) or len(cards) < PAGE_SIZE:  # newest first - older ones follow
                    break
        for o in out:
            o.pop("_cities", None)
            o.pop("_remote", None)
        return out

    def detail(self, offer: dict) -> str | None:
        body = common.get(offer["url"])
        if body is None:
            return None
        text = parse_detail(body)
        return f"{offer['extra']}\n\n{text}".strip() if text and offer.get("extra") else text


_site = Platform("justjoin", "justjoin.it", "jj", "https://justjoin.it/api/candidate-api/offers",
                 "https://justjoin.it/job-offer/{slug}")
NAME, LABEL = _site.name, _site.label
parse_search, search, detail = _site.parse_search, _site.search, _site.detail
