"""LinkedIn public job search (no login)."""

from __future__ import annotations

import re
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from . import common

NAME = "linkedin"
LABEL = "LinkedIn"
SEARCH_URL = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
              "?keywords={kw}&location={loc}&f_TPR=r{secs}&start={start}")
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}/"
PAGE_SIZE = 25


def parse_search(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    offers = []
    for card in soup.select("div.base-card[data-entity-urn]"):
        m = re.search(r"jobPosting:(\d+)", card["data-entity-urn"])
        if not m:
            continue

        def txt(sel):
            el = card.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""
        offers.append({
            "id": m.group(1),  # LinkedIn ids stay unprefixed - compatible with data saved before multi-source
            "source": NAME,
            "title": txt("h3.base-search-card__title"),
            "company": txt("h4.base-search-card__subtitle"),
            "location": txt("span.job-search-card__location"),
            "url": VIEW_URL.format(id=m.group(1)),
        })
    return offers


def parse_detail(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    desc = soup.select_one("div.show-more-less-html__markup")
    text = desc.get_text("\n", strip=True) if desc else ""
    criteria = [
        f"{li.select_one('h3').get_text(strip=True)}: {li.select_one('span').get_text(strip=True)}"
        for li in soup.select("li.description__job-criteria-item")
        if li.select_one("h3") and li.select_one("span")
    ]
    return (text + "\n\n" + "\n".join(criteria)).strip()


def search(searches: list[dict], cfg: dict, hours: int) -> list[dict]:
    out = []
    for s in searches:
        for page in range(int(cfg.get("pages_per_search", 1))):
            url = SEARCH_URL.format(kw=quote_plus(s["keywords"]), loc=quote_plus(s["location"]),
                                    secs=hours * 3600, start=page * PAGE_SIZE)
            if s.get("remote"):
                url += "&f_WT=2"  # LinkedIn filter: remote only
            body = common.get(url)
            if not body:
                break
            cards = parse_search(body)
            for o in cards:
                o["search_keyword"] = s["keywords"]
            out += cards
            time.sleep(2)
            if len(cards) < PAGE_SIZE:
                break
    return out


def detail(offer: dict) -> str | None:
    body = common.get(DETAIL_URL.format(id=offer["id"]))
    return None if body is None else parse_detail(body)
