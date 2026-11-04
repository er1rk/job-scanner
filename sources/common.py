"""Shared helpers for all job sources: HTTP, text cleanup, cities, cross-portal fingerprints."""

from __future__ import annotations

import html as html_lib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}


def get(url: str, retries: int = 3, **kwargs) -> str | None:
    """GET with retries. Returns the body, or None on a network error / non-200 answer."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={**HEADERS, **kwargs.pop("headers", {})}, timeout=25, **kwargs)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:  # rate limited - back off
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(5)
    return None


def post_json(url: str, payload: dict, content_type: str = "application/json", retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=25,
                              headers={**HEADERS, "Content-Type": content_type, "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(5)
    return None


def get_json(url: str, **kwargs):
    body = get(url, headers={"Accept": "application/json", **kwargs.pop("headers", {})}, **kwargs)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def next_data(page: str) -> dict | None:
    """The JSON Next.js embeds in server-rendered pages."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def strip_accents(s: str) -> str:
    s = s.replace("ł", "l").replace("Ł", "L")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def utc_from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_from_ms(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_recent(when: datetime | None, hours: int) -> bool:
    """Unknown dates count as recent - better to score one offer too many than to miss one."""
    if when is None:
        return True
    return (datetime.now(timezone.utc) - when).total_seconds() <= hours * 3600


# ---------------------------------------------------------------- cities
# Config locations are written for LinkedIn ("Warsaw, Poland"); Polish portals want Polish names.
CITY_ALIASES = {
    "warsaw": "Warszawa", "cracow": "Kraków", "krakow": "Kraków", "lodz": "Łódź", "poznan": "Poznań",
    "wroclaw": "Wrocław", "gdansk": "Gdańsk", "szczecin": "Szczecin", "katowice": "Katowice",
    "lublin": "Lublin", "bialystok": "Białystok", "gdynia": "Gdynia", "bydgoszcz": "Bydgoszcz",
    "torun": "Toruń", "rzeszow": "Rzeszów", "kielce": "Kielce", "olsztyn": "Olsztyn", "opole": "Opole",
    "sopot": "Sopot", "gliwice": "Gliwice", "czestochowa": "Częstochowa", "radom": "Radom",
    "zielona gora": "Zielona Góra",
}
POLISH_CITIES = {strip_accents(v).lower() for v in CITY_ALIASES.values()}


def polish_city(location: str) -> str:
    """'Warsaw, Poland' -> 'Warszawa'; 'Poznań, Poland' -> 'Poznań'."""
    name = location.split(",")[0].strip()
    return CITY_ALIASES.get(strip_accents(name).lower(), name)


def city_key(name: str | None) -> str:
    """Comparable form of a city name in any language/spelling: 'Warsaw', 'Warszawa' -> 'warszawa'."""
    if not name:
        return ""
    base = strip_accents(name.split(",")[0]).lower().strip()
    return strip_accents(CITY_ALIASES.get(base, base)).lower()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(s).lower()).strip("-")


# ---------------------------------------------------------------- de-duplication
LEGAL_FORMS = r"\b(sp\.?\s*z\s*o\.?\s*o\.?|sp\.?\s*k\.?|s\.?\s*a\.?|spolka akcyjna|spolka z ograniczona odpowiedzialnoscia|" \
              r"gmbh|ltd\.?|limited|inc\.?|llc|plc|b\.?v\.?|ag|s\.?r\.?l\.?|oddzial w polsce|polska|poland)\b"
GENDER_TAGS = r"\((?:[mkfdx]\s*/\s*[mkfdx](?:\s*/\s*[mkfdx])?|she/he/they|he/she/they|all genders|k/m/n)\)|\b[mkf]/[mkf](?:/[dx])?\b"


def normalise(s: str | None) -> str:
    s = strip_accents((s or "").lower())
    s = re.sub(GENDER_TAGS, " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(company: str | None, title: str | None) -> str:
    """Same company + same title = same job, whichever portal it came from."""
    comp = normalise(re.sub(LEGAL_FORMS, " ", strip_accents((company or "").lower())))
    return f"{comp}|{normalise(title)}"
