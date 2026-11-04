"""Job sources. Each module exposes NAME, LABEL, search(searches, cfg, hours) and detail(offer).

search  -> list of offers: {id, source, title, company, location, url, search_keyword, posted?, extra?}
detail  -> description text; "" if the offer is gone; None on a network error (retried later)
"""

from . import justjoin, linkedin, nofluffjobs, pracuj

ALL = {m.NAME: m for m in (linkedin, pracuj, nofluffjobs, justjoin)}


def label(name: str | None) -> str:
    m = ALL.get(name or "linkedin")
    return m.LABEL if m else (name or "")
