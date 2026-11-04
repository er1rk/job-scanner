"""Offline tests - no network, no API key. Run: python -m unittest -v"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
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
    "role_category": "data/BI analytics", "industry": "fintech", "work_mode": "hybrid",
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
        row = {"id": "1", "title": "Analityk danych", "company": "Żabka", "location": "Poznań",
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

    def test_legacy_seen_migration(self):
        f = Path(self.tmp.name) / "seen.json"
        f.write_text(json.dumps(["a", "b"]))
        self.assertEqual(self.store.import_legacy_seen(f), 2)
        self.assertEqual(self.store.seen_ids(), {"a", "b"})
        self.assertFalse(f.exists())


class EndToEndTests(unittest.TestCase):
    """Full run with LinkedIn, Gemini and SMTP faked."""

    def run_main(self, tmp, argv, env):
        def fake_get(url, retries=3):
            return DETAIL_HTML if "jobPosting/" in url else SEARCH_HTML
        with mock.patch.object(scanner, "DB_FILE", Path(tmp) / "jobs.db"), \
             mock.patch.object(scanner, "SEEN_FILE", Path(tmp) / "seen.json"), \
             mock.patch.object(scanner, "CSV_FILE", Path(tmp) / "jobs.csv"), \
             mock.patch.object(scanner, "get", side_effect=fake_get), \
             mock.patch.object(scanner.requests, "post", side_effect=fake_gemini), \
             mock.patch.object(scanner.time, "sleep"), \
             mock.patch.object(scanner, "send_email") as send, \
             mock.patch.object(sys, "argv", ["scanner.py", *argv]), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x", "CANDIDATE_PROFILE": "profile", **env}):
            scanner.main()
        return send

    def test_full_run_saves_everything_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            send = self.run_main(tmp, [], {"ATTACH_CSV": "true"})
            good, csv_text, warning = send.call_args.args
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


if __name__ == "__main__":
    unittest.main()
