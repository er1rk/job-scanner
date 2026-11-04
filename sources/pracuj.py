"""pracuj.pl - offers are read from the JSON the site embeds in its search and offer pages."""

from __future__ import annotations

import time
from urllib.parse import quote

from . import common

NAME = "pracuj"
LABEL = "pracuj.pl"
PAGE_SIZE = 50
SEARCH_URL = "https://www.pracuj.pl/praca/{kw};kw{where}?rd=0&p={period}{extra}"


def _period(hours: int) -> int:
    """pracuj.pl publication filter: 1, 3, 7, 14 or 30 days."""
    for days in (1, 3, 7, 14, 30):
        if hours <= days * 24:
            return days
    return 30


def _queries(nd: dict) -> list[dict]:
    try:
        return [q["state"].get("data") for q in nd["props"]["pageProps"]["dehydratedState"]["queries"]]
    except (KeyError, TypeError):
        return []


def parse_search(page: str) -> list[dict]:
    """All grouped offers on the page (a group = one job published in one or more locations)."""
    nd = common.next_data(page) or {}
    out = []
    for data in _queries(nd):
        if not (isinstance(data, dict) and isinstance(data.get("groupedOffers"), list)):
            continue
        for g in data["groupedOffers"]:
            offers = g.get("offers") or []
            if not offers or not offers[0].get("partitionId"):
                continue
            facts = [g.get("salaryDisplayText"), ", ".join(g.get("positionLevels") or []),
                     ", ".join(g.get("typesOfContract") or []), ", ".join(g.get("workModes") or [])]
            out.append({
                "id": f"pracuj:{offers[0]['partitionId']}",
                "source": NAME,
                "title": g.get("jobTitle", ""),
                "company": g.get("companyName", ""),
                "location": ", ".join(dict.fromkeys(o.get("displayWorkplace", "") for o in offers if o.get("displayWorkplace"))),
                "url": offers[0].get("offerAbsoluteUri", ""),
                "posted": g.get("lastPublicated"),
                "extra": " | ".join(f for f in facts if f),
            })
    return out


def parse_detail(page: str) -> str:
    nd = common.next_data(page) or {}
    for data in _queries(nd):
        if isinstance(data, dict) and data.get("textSections"):
            parts = [s.get("plainText", "") for s in data["textSections"] if s.get("plainText")]
            salary = []

            def walk(o, key=""):
                if isinstance(o, dict):
                    for k, v in o.items():
                        walk(v, k)
                elif isinstance(o, list):
                    for v in o:
                        walk(v, key)
                elif isinstance(o, str) and "salar" in key.lower() and o.strip():
                    salary.append(o.strip())
            walk(data.get("attributes") or {})
            if salary:
                parts.insert(0, "Salary: " + "; ".join(dict.fromkeys(salary)))
            return "\n\n".join(parts).strip()
    return ""


def search(searches: list[dict], cfg: dict, hours: int) -> list[dict]:
    out, period = [], _period(hours)
    for s in searches:
        if s.get("remote"):
            where, extra = "", "&wm=home-office"
        else:
            where, extra = f"/{common.slug(common.polish_city(s['location']))};wp", ""
        for page in range(1, int(cfg.get("pages_per_search", 1)) + 1):
            url = SEARCH_URL.format(kw=quote(s["keywords"]), where=where, period=period,
                                    extra=extra + (f"&pn={page}" if page > 1 else ""))
            body = common.get(url)
            if not body:
                break
            cards = parse_search(body)
            for o in cards:
                o["search_keyword"] = s["keywords"]
            out += [o for o in cards if common.is_recent(common.utc_from_iso(o.get("posted")), hours)]
            time.sleep(1.5)
            if len(cards) < PAGE_SIZE:
                break
    return out


def detail(offer: dict) -> str | None:
    body = common.get(offer["url"])
    if body is None:
        return None
    text = parse_detail(body)
    return f"{offer['extra']}\n\n{text}".strip() if text and offer.get("extra") else text
