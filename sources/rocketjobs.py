"""rocketjobs.pl - justjoin.it's sister site for non-IT jobs (finance, compliance, analytics, operations).
Same API and page format as justjoin.it, so the implementation is shared."""

from .justjoin import Platform

_site = Platform("rocketjobs", "RocketJobs", "rj", "https://rocketjobs.pl/api/candidate-api/offers",
                 "https://rocketjobs.pl/oferta-pracy/{slug}")
NAME, LABEL = _site.name, _site.label
parse_search, search, detail = _site.parse_search, _site.search, _site.detail
