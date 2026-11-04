"""Offline tests - no network, no API key. Run: python -m unittest -v"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import report
import scanner
import sources
from sources import common, justjoin, nofluffjobs, pracuj, rocketjobs
from storage import Store

SEARCH_HTML = """
<div class="base-card" data-entity-urn="urn:li:jobPosting:111">
  <h3 class="base-search-card__title">Data Analyst</h3>
  <h4 class="base-search-card__subtitle">Acme</h4>
  <span class="job-search-card__location">Warsaw, Poland</span></div>
<div class="base-card" data-entity-urn="urn:li:jobPosting:222">
  <h3 class="base-search-card__title">Senior Data Analyst</h3>
  <h4 class="base-search-card__subtitle">Big Corp</h4>
  <span class="job-search-card__location">Warsaw, Poland</span></div>
"""
DETAIL_HTML = """<div class="show-more-less-html__markup">Power BI, Excel. 8000-11000 PLN brutto.</div>
<ul><li class="description__job-criteria-item"><h3>Seniority level</h3><span>Entry level</span></li></ul>"""

GEMINI_RESULT = {
    "score": 82, "verdict": "apply", "seniority": "junior",
    "strengths": ["Power BI", "2 years data reporting"], "gaps": ["SQL"],
    "one_line_reason": "Strong reporting fit.",
    "role_category": "data/analytics", "industry": "retail", "work_mode": "hybrid",
    "contract_types": ["employment contract"], "salary_min": 8000, "salary_max": 11000,
    "salary_currency": "PLN", "salary_period": "month", "salary_type": "gross",
    "years_required": 1, "english_level": "B2", "polish_required": True,
    "required_tools": ["Power BI", "Excel"], "nice_to_have_tools": ["SQL"],
}


def fake_gemini(*args, **kwargs):
    r = mock.Mock(status_code=200)
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(GEMINI_RESULT)}]}}]}
    return r


class SalaryTests(unittest.TestCase):
    cfg = {"fx_to_pln": {"PLN": 1, "EUR": 4.25}}

    def test_month_pln(self):
        out = scanner.normalise_salary({"salary_min": 8000, "salary_max": 11000,
                                        "salary_currency": "PLN", "salary_period": "month"}, self.cfg)
        self.assertEqual(out, {"salary_min_pln_month": 8000, "salary_max_pln_month": 11000})

    def test_hourly_eur_and_symbols(self):
        out = scanner.normalise_salary({"salary_min": 20, "salary_max": None,
                                        "salary_currency": "€", "salary_period": "hour"}, self.cfg)
        self.assertEqual(out["salary_min_pln_month"], round(20 * 168 * 4.25))
        self.assertIsNone(out["salary_max_pln_month"])

    def test_unknown_currency_or_missing(self):
        self.assertIsNone(scanner.normalise_salary({"salary_min": 5, "salary_currency": "XYZ"},
                                                   self.cfg)["salary_min_pln_month"])
        self.assertIsNone(scanner.normalise_salary({}, self.cfg)["salary_min_pln_month"])

    def test_label(self):
        self.assertEqual(scanner.salary_label(GEMINI_RESULT), "8 000–11 000 PLN / month gross")
        self.assertEqual(scanner.salary_label({}), "")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_export_and_keep_labels_on_rescore(self):
        row = {"id": "1", "title": "Analityk danych", "company": "Żabka", "location": "Kraków",
               "url": "u", **GEMINI_RESULT}
        self.store.save_offer(row, "opis oferty")
        self.store.con.execute("UPDATE offers SET user_label='applied', first_seen='2026-01-01' WHERE id='1'")
        self.store.save_offer({**row, "score": 50})  # re-score
        r = self.store.con.execute("SELECT score, user_label, first_seen FROM offers").fetchone()
        self.assertEqual(tuple(r), (50, "applied", "2026-01-01"))
        self.assertEqual(self.store.description("1"), "opis oferty")
        csv_text = self.store.export_csv()
        self.assertTrue(csv_text.startswith("﻿id,"))
        self.assertIn("Power BI | Excel", csv_text)
        self.assertIn("Żabka", csv_text)

    def test_queue_expiry_and_old_database_upgrade(self):
        self.store.enqueue([{"id": "q1", "title": "t"}])
        self.store.con.execute("UPDATE queue SET queued_at = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(self.store.expire_queue(7), 1)
        self.assertIn("q1", self.store.seen_ids())
        # a database created before the queue existed gets the new run columns
        path = Path(self.tmp.name) / "old.db"
        import sqlite3
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE runs (run_id INTEGER PRIMARY KEY, started_at TEXT)")
        con.commit(); con.close()
        old = Store(path)
        cols = {r[1] for r in old.con.execute("PRAGMA table_info(runs)")}
        self.assertTrue({"queued", "queue_expired"} <= cols)
        old.close()

    def test_legacy_seen_migration(self):
        f = Path(self.tmp.name) / "seen.json"
        f.write_text(json.dumps(["a", "b"]))
        self.assertEqual(self.store.import_legacy_seen(f), 2)
        self.assertEqual(self.store.seen_ids(), {"a", "b"})
        self.assertFalse(f.exists())


class EndToEndTests(unittest.TestCase):
    """Full run with LinkedIn, Gemini and SMTP faked."""

    def run_main(self, tmp, argv, env, search_html=SEARCH_HTML, detail=lambda url: DETAIL_HTML,
                 gemini=fake_gemini, **cfg_overrides):
        cfg = scanner.load_config()
        cfg.update(attach_csv_weekdays=[], report_weekdays=[], sources=["linkedin"])
        cfg.update(cfg_overrides)

        def fake_get(url, retries=3, **kwargs):
            return detail(url) if "jobPosting/" in url else search_html
        with mock.patch.object(scanner, "DB_FILE", Path(tmp) / "jobs.db"), \
             mock.patch.object(scanner, "SEEN_FILE", Path(tmp) / "seen.json"), \
             mock.patch.object(scanner, "CSV_FILE", Path(tmp) / "jobs.csv"), \
             mock.patch.object(scanner, "DB_ENC_FILE", Path(tmp) / "jobs.db.enc"), \
             mock.patch.object(common, "get", side_effect=fake_get), \
             mock.patch.object(scanner.requests, "post", side_effect=gemini), \
             mock.patch.object(scanner.time, "sleep"), \
             mock.patch.object(scanner, "load_config", return_value=cfg), \
             mock.patch.object(scanner, "send_weekly_report") as weekly, \
             mock.patch.object(scanner, "send_email") as send, \
             mock.patch.object(sys, "argv", ["scanner.py", *argv]), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x", "CANDIDATE_PROFILE": "profile",
                                          "GITHUB_ACTIONS": "", "DB_ENCRYPTION_KEY": "", **env}):
            scanner.main()
        self.weekly = weekly
        return send

    def test_full_run_saves_everything_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            send = self.run_main(tmp, [], {"ATTACH_CSV": "true"})
            good, csv_text, warning, waiting = send.call_args.args
            self.assertEqual(waiting, 0)
            self.assertEqual([r["id"] for r in good], ["111"])  # "Senior" offer dropped by title
            self.assertIn("8000", csv_text)
            self.assertEqual(warning, "")
            store = Store(Path(tmp) / "jobs.db")
            self.assertEqual(store.count(), 1)
            run = store.con.execute("SELECT cards_found, filtered_title, scored, notified FROM runs").fetchone()
            store.close()
            # LinkedIn search is called once per (keyword x location) - same 2 cards each time
            self.assertEqual(run["filtered_title"] >= 1, True)
            self.assertEqual((run["scored"], run["notified"]), (1, 1))

            send2 = self.run_main(tmp, [], {"ATTACH_CSV": "false"})  # second run: nothing new
            send2.assert_not_called()

            self.run_main(tmp, ["--export"], {})
            self.assertIn("Data Analyst", (Path(tmp) / "jobs.csv").read_text(encoding="utf-8"))

    def test_report_day_moves_csv_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            send = self.run_main(tmp, [], {"ATTACH_CSV": "true", "SEND_REPORT": "true"})
            self.assertIsNone(send.call_args.args[1])           # digest without CSV
            self.assertIn("8000", self.weekly.call_args.args[4])  # report carries it

    def test_overflow_is_queued_and_scored_first_next_run(self):
        cards = "".join(f'''<div class="base-card" data-entity-urn="urn:li:jobPosting:{i}">
            <h3 class="base-search-card__title">Analyst {i}</h3><h4 class="base-search-card__subtitle">C</h4>
            <span class="job-search-card__location">L</span></div>''' for i in (1, 2, 3))
        with tempfile.TemporaryDirectory() as tmp:
            send = self.run_main(tmp, [], {}, search_html=cards, max_offers_per_run=1)
            self.assertEqual([r["id"] for r in send.call_args.args[0]], ["1"])
            self.assertEqual(send.call_args.args[3], 2)                  # 2 waiting, mentioned in the e-mail
            store = Store(Path(tmp) / "jobs.db")
            self.assertEqual([o["id"] for o in store.queued()], ["2", "3"])
            store.close()
            # next run: LinkedIn shows nothing new, the queue is worked off oldest first
            send = self.run_main(tmp, [], {}, search_html="", max_offers_per_run=1)
            self.assertEqual([r["id"] for r in send.call_args.args[0]], ["2"])
            send = self.run_main(tmp, [], {}, search_html="", max_offers_per_run=5)
            self.assertEqual([r["id"] for r in send.call_args.args[0]], ["3"])
            store = Store(Path(tmp) / "jobs.db")
            self.assertEqual((store.count(), store.queue_size()), (3, 0))
            run = store.con.execute("SELECT queued FROM runs ORDER BY run_id").fetchall()
            self.assertEqual([r[0] for r in run], [2, 1, 0])
            store.close()

    def test_failed_scoring_is_retried_then_dropped(self):
        def broken_gemini(*a, **k):
            return mock.Mock(status_code=400, text="bad request")
        with tempfile.TemporaryDirectory() as tmp:
            for attempt in range(3):
                self.run_main(tmp, [], {}, gemini=broken_gemini)
                store = Store(Path(tmp) / "jobs.db")
                self.assertEqual(store.queue_size(), 0 if attempt == 2 else 1)
                store.close()
            store = Store(Path(tmp) / "jobs.db")
            self.assertIn("111", store.seen_ids())                     # given up, not retried forever
            store.close()

    def test_removed_offer_leaves_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_main(tmp, [], {}, detail=lambda url: None)          # network error -> queued
            store = Store(Path(tmp) / "jobs.db"); self.assertEqual(store.queue_size(), 1); store.close()
            self.run_main(tmp, [], {}, search_html="", detail=lambda url: "<html></html>")  # offer gone
            store = Store(Path(tmp) / "jobs.db")
            self.assertEqual((store.queue_size(), store.count()), (0, 0))
            store.close()

    def test_encrypted_database_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DB_ENCRYPTION_KEY": "a long passphrase made up just for this test"}
            self.run_main(tmp, [], env)
            self.assertFalse((Path(tmp) / "jobs.db").exists())         # plaintext removed
            enc = (Path(tmp) / "jobs.db.enc").read_bytes()
            self.assertNotIn(b"Data Analyst", enc)
            self.assertNotIn(b"SQLite", enc)
            send2 = self.run_main(tmp, [], env)                         # decrypts -> offer already known
            send2.assert_not_called()
            with self.assertRaises(SystemExit):
                self.run_main(tmp, [], {"DB_ENCRYPTION_KEY": "wrong key"})

    def test_ci_requires_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_main(tmp, [], {"GITHUB_ACTIONS": "true"})
            self.assertFalse((Path(tmp) / "jobs.db").exists())

    def test_dry_run_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            send = self.run_main(tmp, ["--dry-run"], {})
            send.assert_not_called()
            self.assertFalse((Path(tmp) / "jobs.db").exists())

    def test_email_html(self):
        body = scanner.build_email([{"id": "1", "title": "A & B", "company": "C", "location": "L",
                                     "url": "https://x", **GEMINI_RESULT}], "csv", "")
        self.assertIn("A &amp; B", body)
        self.assertIn("8 000–11 000 PLN / month gross · hybrid · employment contract · EN B2", body)
        self.assertIn("jobs.csv", body)
        self.assertIn("0 offers", scanner.build_email([], None, "found 0 offers"))


class PrivateConfigTests(unittest.TestCase):
    def test_private_config_overrides_public_keys(self):
        private = "keywords: [secret role]\nrole_categories: [alpha, beta]\n"
        with mock.patch.dict(os.environ, {"PRIVATE_CONFIG": private}):
            cfg = scanner.load_config()
        self.assertEqual(cfg["keywords"], ["secret role"])
        self.assertIn("gemini_model", cfg)                              # public keys kept
        enum = scanner.build_schema(cfg)["properties"]["role_category"]["enum"]
        self.assertEqual(enum, ["alpha", "beta", "other"])
        self.assertEqual(scanner.SCHEMA["properties"]["role_category"]["enum"],
                         scanner.DEFAULT_ROLE_CATEGORIES)               # global schema untouched

    def test_invalid_private_config(self):
        with mock.patch.dict(os.environ, {"PRIVATE_CONFIG": "- just a list"}):
            with self.assertRaises(SystemExit):
                scanner.load_config()

    def test_fernet_key_also_accepted(self):
        from cryptography.fernet import Fernet
        from storage import decrypt_file, encrypt_file
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = (Path(tmp) / n for n in "abc")
            a.write_bytes(b"hello")
            key = Fernet.generate_key().decode()
            encrypt_file(a, b, key)
            decrypt_file(b, c, key)
            self.assertEqual(c.read_bytes(), b"hello")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")
        from datetime import datetime, timedelta, timezone
        self.now = datetime.now(timezone.utc)
        def add(i, score, gaps, days_ago, tools=("Excel",), cat="data/analytics", sal=None):
            self.store.save_offer({"id": str(i), "title": f"Job {i}", "company": "C", "url": f"https://x/{i}",
                                   "score": score, "verdict": "apply" if score >= 60 else "stretch",
                                   "strengths": [], "gaps": list(gaps), "one_line_reason": "",
                                   "role_category": cat, "work_mode": "hybrid", "english_level": "B2",
                                   "required_tools": list(tools), "salary_min_pln_month": sal,
                                   "salary_max_pln_month": sal and sal + 2000,
                                   "first_seen": (self.now - timedelta(days=days_ago)).isoformat()})
        # this week: 12 offers
        for i in range(6):
            add(i, 50, ["SQL", "German B2"] if i < 3 else ["sql"], 1, tools=("SQL", "Excel"), sal=8000 + i * 100)
        for i in range(6, 12):
            add(i, 75, ["Python"], 2, cat="finance")
        # previous week: 2 offers with SQL gap, plus one too old to count
        add(20, 50, ["SQL"], 9)
        add(21, 50, ["SQL"], 10)
        add(22, 50, ["SAP"], 30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stats(self):
        st = report.compute_stats(self.store, threshold=60, now=self.now)
        self.assertEqual((st["offers"], st["offers_prev_week"], st["offers_total"]), (12, 2, 15))
        self.assertEqual(st["near_miss_offers"], 6)                     # scores 40-59
        top = st["top_gaps"][0]
        self.assertEqual((top["label"], top["count"], top["prev"]), ("SQL", 6, 2))  # case-insensitive merge
        self.assertEqual([g["label"] for g in st["near_miss_gaps"]], ["SQL", "German B2"])
        sql_tool = next(t for t in st["top_required_tools"] if t["label"] == "SQL")
        self.assertEqual((sql_tool["count"], sql_tool["as_gap"]), (6, 6))
        cat = st["categories"][0]
        self.assertEqual((cat["count"], cat["with_salary"], cat["median_min"]), (6, 6, 8250))
        self.assertEqual(st["offers_with_salary_pct"], 50)

    def test_ai_gets_only_aggregates_and_is_capped(self):
        st = report.compute_stats(self.store, threshold=60, now=self.now)
        seen = {}
        def fake_call(prompt, schema):
            seen["prompt"] = prompt
            return {"headline": "h", "market_note": "m",
                    "recommendations": [{"skill": s, "why": "w", "first_step": "f"} for s in "ABCD"]}
        recs = report.ai_recommendations(st, "profile", fake_call)
        self.assertEqual(len(recs["recommendations"]), 3)
        self.assertNotIn("https://x/", seen["prompt"])
        html_out = report.build_report_html(st, recs, csv_attached=True)
        self.assertIn("What blocks near-miss offers", html_out)
        self.assertIn("jobs.csv", html_out)

    def test_small_sample_skips_ai(self):
        st = report.compute_stats(self.store, threshold=60, now=self.now, days=1)  # only 0-1 day old offers
        self.assertLess(st["offers"], report.MIN_OFFERS_FOR_AI)
        self.assertIsNone(report.ai_recommendations(st, "p", lambda *a: self.fail("AI called")))

    def test_empty_week(self):
        empty = Store(Path(self.tmp.name) / "empty.db")
        st = report.compute_stats(empty, threshold=60)
        self.assertIn("No offers", report.build_report_html(st))
        empty.close()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- sources (formats recorded from the live sites)
def next_page(data: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


PRACUJ_SEARCH = next_page({"props": {"pageProps": {"dehydratedState": {"queries": [
    {"state": {"data": {"offersTotalCount": 2, "groupedOffers": [
        {"jobTitle": "Data Analyst (k/m)", "companyName": "Acme Sp. z o.o.", "lastPublicated": "2099-01-01T08:00:00Z",
         "salaryDisplayText": "9 000–12 000 zł brutto / mies.", "positionLevels": ["Specjalista (Mid)"],
         "typesOfContract": ["umowa o pracę"], "workModes": ["praca hybrydowa"],
         "offers": [{"partitionId": 555, "offerAbsoluteUri": "https://www.pracuj.pl/praca/x,oferta,555",
                     "displayWorkplace": "Warszawa"}]},
        {"jobTitle": "Old offer", "companyName": "B", "lastPublicated": "2001-01-01T08:00:00Z",
         "offers": [{"partitionId": 556, "offerAbsoluteUri": "u", "displayWorkplace": "Warszawa"}]},
    ]}}},
    {"state": {"data": [{"code": "1", "name": "24h"}]}},
]}}}})
PRACUJ_DETAIL = next_page({"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {
    "attributes": {"employment": {"salaryText": "9000-12000 PLN"}},
    "textSections": [{"plainText": "O projekcie, analiza danych"}, {"plainText": "Wymagania, Power BI, SQL"}]}}}]}}}})

NFJ_SEARCH = {"totalPages": 1, "postings": [
    {"id": "data-analyst-acme-warszawa", "title": "Data Analyst", "name": "ACME", "posted": 4070908800000,
     "url": "data-analyst-acme-warszawa", "location": {"places": [{"city": "Warszawa"}]},
     "salary": {"from": 10000, "to": 14000, "currency": "PLN", "type": "b2b"}, "seniority": ["Mid"]},
    {"id": "old", "title": "Old", "name": "X", "posted": 978307200000, "location": {"places": []}},
    {"id": "bumped", "title": "Bumped BI Analyst", "name": "Y", "posted": 978307200000, "renewed": 4070908800000,
     "location": {"places": [{"city": "Warszawa"}]}},
]}
NFJ_DETAIL = {"basics": {"seniority": ["Mid"]},
              "requirements": {"musts": [{"value": "SQL"}, {"value": "Power BI"}], "nices": [{"value": "Python"}],
                               "languages": [{"code": "en", "type": "MUST"}], "description": "<ul><li>2 lata</li></ul>"},
              "specs": {"dailyTasks": ["Raporty"]}, "details": {"description": "<p>Opis <b>firmy</b></p>"},
              "essentials": {"originalSalary": {"types": {"b2b": {"period": "Month", "range": [10000, 14000]}}}}}

JJ_SEARCH = {"data": [
    {"slug": "acme-data-analyst-warszawa", "title": "Data Analyst", "companyName": "Acme", "city": "Warsaw",
     "workplaceType": "hybrid", "experienceLevel": "mid", "publishedAt": "2099-01-01T08:00:00Z", "locations": [],
     "requiredSkills": [{"name": "SQL"}], "employmentTypes": [{"from": 9000, "to": 12000, "currency": "pln",
                                                             "currencySource": "original", "unit": "month", "type": "permanent", "gross": True}]},
    {"slug": "remote-pl", "title": "BI Analyst", "companyName": "R", "city": "Gdansk", "workplaceType": "remote",
     "publishedAt": "2099-01-01T07:00:00Z", "locations": []},
    {"slug": "remote-abroad", "title": "BI Analyst", "companyName": "L", "city": "Lisbon", "workplaceType": "remote",
     "publishedAt": "2099-01-01T06:00:00Z", "locations": []},
    {"slug": "office-krakow", "title": "Analyst", "companyName": "K", "city": "Krakow", "workplaceType": "office",
     "publishedAt": "2099-01-01T05:00:00Z", "locations": []},
]}
JJ_DETAIL = ('<script type="application/ld+json">{"@type": "JobPosting", "description": "<p>We need <b>SQL</b></p>"}'
             '</script>')
SEARCHES = [{"keywords": "data analyst", "location": "Warsaw, Poland"},
            {"keywords": "data analyst", "location": "Poland", "remote": True}]


class CommonTests(unittest.TestCase):
    def test_fingerprint_matches_across_portals(self):
        a = common.fingerprint("Acme Sp. z o.o.", "Data Analyst (k/m)")
        self.assertEqual(a, common.fingerprint("ACME", "Data Analyst"))
        self.assertEqual(common.fingerprint("Żabka Polska S.A.", "Analityk danych (m/k)"),
                         common.fingerprint("Zabka", "Analityk Danych"))
        self.assertNotEqual(a, common.fingerprint("Acme", "Senior Data Analyst"))

    def test_cities(self):
        self.assertEqual(common.polish_city("Warsaw, Poland"), "Warszawa")
        self.assertEqual(common.slug(common.polish_city("Łódź, Poland")), "lodz")
        self.assertEqual(common.city_key("Warsaw"), common.city_key("Warszawa"))
        self.assertEqual(common.city_key("Poznan"), common.city_key("Poznań, Poland"))

    def test_round_robin(self):
        self.assertEqual(scanner.round_robin([[1, 2, 3], ["a"], ["x", "y"]]), [1, "a", "x", 2, "y", 3])


class SourceParsingTests(unittest.TestCase):
    def test_pracuj(self):
        with mock.patch.object(common, "get", side_effect=lambda u, **k: PRACUJ_DETAIL if "oferta" in u else PRACUJ_SEARCH), \
             mock.patch("time.sleep"):
            offers = pracuj.search(SEARCHES[:1], {"pages_per_search": 1}, 24)
            self.assertEqual([o["id"] for o in offers], ["pracuj:555"])  # old one filtered by date
            o = offers[0]
            self.assertEqual((o["company"], o["location"], o["source"]), ("Acme Sp. z o.o.", "Warszawa", "pracuj"))
            text = pracuj.detail(o)
        self.assertIn("9 000–12 000 zł", text)
        self.assertIn("Salary: 9000-12000 PLN", text)
        self.assertIn("Wymagania, Power BI", text)
        self.assertEqual(pracuj._period(24), 1)
        self.assertEqual(pracuj._period(72), 3)

    def test_pracuj_urls(self):
        urls = []
        with mock.patch.object(common, "get", side_effect=lambda u, **k: urls.append(u) or PRACUJ_SEARCH), \
             mock.patch("time.sleep"):
            pracuj.search(SEARCHES, {"pages_per_search": 1}, 24)
        self.assertIn("/praca/data%20analyst;kw/warszawa;wp?rd=0&p=1", urls[0])
        self.assertTrue(urls[1].endswith("data%20analyst;kw?rd=0&p=1&wm=home-office"))

    def test_nofluffjobs(self):
        bodies = []
        def fake_post(url, payload, content_type=None):
            bodies.append(payload["rawSearch"])
            return NFJ_SEARCH
        with mock.patch.object(common, "post_json", side_effect=fake_post), \
             mock.patch.object(common, "get_json", return_value=NFJ_DETAIL), mock.patch("time.sleep"):
            offers = nofluffjobs.search(SEARCHES, {"pages_per_search": 1}, 24)
            text = nofluffjobs.detail(offers[0])
        self.assertEqual(bodies, ["keyword='data analyst' city=warszawa", "keyword='data analyst' city=remote"])
        self.assertEqual({o["id"] for o in offers}, {"nfj:data-analyst-acme-warszawa", "nfj:bumped"})
        self.assertEqual(offers[0]["url"], "https://nofluffjobs.com/pl/job/data-analyst-acme-warszawa")
        for part in ("Must have: SQL, Power BI", "Nice to have: Python", "en (MUST)", "- Raporty", "Opis firmy",
                     "b2b: [10000, 14000] per Month", "10000-14000 PLN (b2b)"):
            self.assertIn(part, text)

    def test_justjoin_location_filter_and_detail(self):
        with mock.patch.object(common, "get_json", return_value=JJ_SEARCH), \
             mock.patch.object(common, "get", return_value=JJ_DETAIL), mock.patch("time.sleep"):
            offers = justjoin.search(SEARCHES, {"pages_per_search": 1}, 24)
            ids = [o["id"] for o in offers]
            # Warsaw kept, remote in Poland kept, remote abroad and office in another city dropped
            self.assertEqual(ids, ["jj:acme-data-analyst-warszawa", "jj:remote-pl"])
            self.assertNotIn("_cities", offers[0])
            text = justjoin.detail(offers[0])
        self.assertIn("We need SQL", text)
        self.assertIn("9000-12000 PLN /month permanent gross", text)
        self.assertIn("Required skills: SQL", text)

    def test_justjoin_without_remote(self):
        with mock.patch.object(common, "get_json", return_value=JJ_SEARCH), mock.patch("time.sleep"):
            offers = justjoin.search(SEARCHES[:1], {"pages_per_search": 1}, 24)
        self.assertEqual([o["id"] for o in offers], ["jj:acme-data-analyst-warszawa"])


class RocketJobsTests(unittest.TestCase):
    def test_same_platform_different_site(self):
        urls = []
        def fake_json(url, **kw):
            urls.append(url)
            return JJ_SEARCH
        with mock.patch.object(common, "get_json", side_effect=fake_json), \
             mock.patch.object(common, "get", return_value=JJ_DETAIL), mock.patch("time.sleep"):
            offers = rocketjobs.search(SEARCHES, {"pages_per_search": 1}, 24)
            text = rocketjobs.detail(offers[0])
        self.assertTrue(urls[0].startswith("https://rocketjobs.pl/api/candidate-api/offers?"))
        self.assertEqual(offers[0]["id"], "rj:acme-data-analyst-warszawa")
        self.assertEqual(offers[0]["url"], "https://rocketjobs.pl/oferta-pracy/acme-data-analyst-warszawa")
        self.assertEqual(offers[0]["source"], "rocketjobs")
        self.assertIn("We need SQL", text)
        self.assertEqual(sources.label("rocketjobs"), "RocketJobs")
        self.assertIn("rocketjobs", sources.ALL)


class MultiSourceRunTests(unittest.TestCase):
    run_main = EndToEndTests.run_main

    def test_cross_portal_duplicate_scored_once_and_title_filter(self):
        linkedin_card = """<div class="base-card" data-entity-urn="urn:li:jobPosting:777">
          <h3 class="base-search-card__title">Data Analyst</h3><h4 class="base-search-card__subtitle">ACME</h4>
          <span class="job-search-card__location">Warsaw</span></div>
          <div class="base-card" data-entity-urn="urn:li:jobPosting:778">
          <h3 class="base-search-card__title">Office Assistant</h3><h4 class="base-search-card__subtitle">Z</h4>
          <span class="job-search-card__location">Warsaw</span></div>"""
        def detail(url):
            return PRACUJ_DETAIL if "oferta" in url else DETAIL_HTML
        def search_get(url, retries=3, **kw):
            if "jobPosting/" in url or "oferta" in url:
                return detail(url)
            return PRACUJ_SEARCH if "pracuj" in url else linkedin_card
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, "get", side_effect=search_get):
            cfg = scanner.load_config()
            cfg.update(attach_csv_weekdays=[], report_weekdays=[], sources=["linkedin", "pracuj"],
                       include_title_keywords=["analyst", "analityk"], keywords=["data analyst"],
                       locations=["Warsaw, Poland"], include_remote_poland=False)
            with mock.patch.object(scanner, "DB_FILE", Path(tmp) / "jobs.db"), \
                 mock.patch.object(scanner, "SEEN_FILE", Path(tmp) / "seen.json"), \
                 mock.patch.object(scanner, "DB_ENC_FILE", Path(tmp) / "jobs.db.enc"), \
                 mock.patch.object(scanner.requests, "post", side_effect=fake_gemini), \
                 mock.patch("time.sleep"), mock.patch.object(scanner, "load_config", return_value=cfg), \
                 mock.patch.object(scanner, "send_weekly_report"), \
                 mock.patch.object(scanner, "send_email") as send, \
                 mock.patch.object(sys, "argv", ["scanner.py"]), \
                 mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x", "CANDIDATE_PROFILE": "p",
                                              "GITHUB_ACTIONS": "", "DB_ENCRYPTION_KEY": ""}):
                scanner.main()
            scored = send.call_args.args[0]
            self.assertEqual([r["id"] for r in scored], ["777"])  # pracuj copy of ACME Data Analyst dropped
            store = Store(Path(tmp) / "jobs.db")
            per = json.loads(store.con.execute("SELECT sources FROM runs").fetchone()[0])
            store.close()
            self.assertEqual(per["linkedin"]["filtered"], 1)        # "Office Assistant" fails include filter
            self.assertEqual(per["pracuj"]["duplicates"], 1)
            self.assertIn("LinkedIn", scanner.build_email(scored))

    def test_broken_source_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sources.pracuj, "search", side_effect=KeyError("layout changed")):
            send = self.run_main(tmp, [], {}, sources=["pracuj", "linkedin"])
            good, _, warning, _ = send.call_args.args
            self.assertEqual([r["id"] for r in good], ["111"])
            self.assertIn("pracuj.pl", warning)
