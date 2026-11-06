"""Web app tests - run headless with Streamlit's AppTest on the demo database (no browser, no Supabase)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).parent / "streamlit_app.py"
sys.path.insert(0, str(APP.parent))
sys.path.insert(0, str(APP.parent.parent))


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import demo_data
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "demo.db")
        demo_data.build(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        with mock.patch.dict(os.environ, {"APP_BACKEND": "local", "LOCAL_DB": self.db}):
            at = AppTest.from_file(str(APP), default_timeout=60).run()
        return at

    def test_all_tabs_render_without_errors(self):
        at = self.run_app()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual([t.label for t in at.tabs], ["Offers", "My applications", "Scanner", "Market"])
        self.assertTrue(any("offers" in c.value for c in at.caption))
        self.assertTrue(any(m.label == "Waiting in queue" and m.value == "2" for m in at.metric))

    def test_settings_saved_from_the_form(self):
        from data import LocalBackend
        with mock.patch.dict(os.environ, {"APP_BACKEND": "local", "LOCAL_DB": self.db}):
            from streamlit.testing.v1 import AppTest
            at = AppTest.from_file(str(APP), default_timeout=60).run()
            kw = next(t for t in at.text_area if t.label.startswith("Keywords"))
            kw.set_value("kyc analyst\naml analyst\n")
            next(b for b in at.button if b.label == "Save settings").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(LocalBackend(self.db).settings()["keywords"], ["kyc analyst", "aml analyst"])

    def test_local_backend_tracking(self):
        from data import LocalBackend
        b = LocalBackend(self.db)
        b.save_application("demo-1", status="applied", applied_on="2026-11-06", follow_up_on="2026-11-13")
        b.save_application("demo-1", notes="phone screen booked")          # partial update keeps the rest
        row = b.applications().set_index("offer_id").loc["demo-1"]
        self.assertEqual((row.status, row.applied_on, row.notes), ("applied", "2026-11-06", "phone screen booked"))
        b.save_setting("min_score_to_notify", 70)
        b.save_setting("locations", [])                                      # empty = back to defaults
        self.assertEqual(b.settings().get("min_score_to_notify"), 70)
        self.assertNotIn("locations", b.settings())
        self.assertIn("keywords", b.effective_settings())


if __name__ == "__main__":
    unittest.main()


class FakeQuery:
    """Minimal stand-in for supabase-py's query builder."""
    def __init__(self, store, table):
        self.store, self.table, self.ops = store, table, []

    def __getattr__(self, name):
        def chain(*a, **k):
            self.ops.append((name, a, k))
            return self
        return chain

    def execute(self):
        self.store.calls.append((self.table, self.ops))
        rng = next((a for n, a, k in self.ops if n == "range"), None)
        rows = self.store.rows.get(self.table, [])
        return mock.Mock(data=rows[rng[0]:rng[1] + 1] if rng else rows)


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, name):
        return FakeQuery(self, name)


class SupabaseBackendTests(unittest.TestCase):
    def test_paginates_and_writes(self):
        from data import SupabaseBackend
        offers = [{"id": str(i), "first_seen": "2026-11-06T06:00:00+00:00", "notified": True, "gaps": ["SQL"],
                   "strengths": [], "contract_types": [], "required_tools": [], "nice_to_have_tools": []}
                  for i in range(1500)]
        c = FakeClient({"offers": offers, "settings": [{"key": "min_score_to_notify", "value": 65}]})
        b = SupabaseBackend(c)
        self.assertEqual(len(b.offers()), 1500)                               # two pages of 1000
        self.assertEqual(b.settings(), {"min_score_to_notify": 65})
        b.save_setting("keywords", [])                                        # empty -> delete the override
        self.assertIn("delete", [n for n, *_ in c.calls[-1][1]])
        b.save_application("7", status="applied", bogus="x")
        upsert = next(a for n, a, k in c.calls[-1][1] if n == "upsert")[0]
        self.assertEqual((upsert["offer_id"], upsert["status"]), ("7", "applied"))
        self.assertNotIn("bogus", upsert)


class EmptySupabaseTests(unittest.TestCase):
    """A fresh Supabase project before the first sync: every table empty (they come back without columns)."""
    def test_app_renders_with_empty_tables(self):
        from data import SupabaseBackend
        from streamlit.testing.v1 import AppTest
        env = {k: v for k, v in os.environ.items() if k not in ("APP_BACKEND", "LOCAL_DB")}
        with mock.patch.dict(os.environ, env, clear=True):
            at = AppTest.from_file(str(APP), default_timeout=60)
            at.session_state["backend"] = SupabaseBackend(FakeClient({}))
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        texts = " ".join(i.value for i in at.info)
        self.assertIn("No offers yet", texts)
        self.assertIn("No applications tracked yet", texts)

    def test_applications_without_their_offers(self):
        """An application whose offer isn't synced yet must not break the tracker."""
        from data import SupabaseBackend
        from streamlit.testing.v1 import AppTest
        env = {k: v for k, v in os.environ.items() if k not in ("APP_BACKEND", "LOCAL_DB")}
        with mock.patch.dict(os.environ, env, clear=True):
            at = AppTest.from_file(str(APP), default_timeout=60)
            at.session_state["backend"] = SupabaseBackend(FakeClient(
                {"applications": [{"offer_id": "x", "status": "applied", "applied_on": "2026-11-01"}]}))
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])


class MissingValuesTests(unittest.TestCase):
    """Real offers often lack some fields (salary without period/currency, no work mode...). pandas 3 turns
    missing text into NaN, which crashed the Offers page on Streamlit Cloud."""
    OFFERS = [
        {"id": "a", "source": "pracuj", "title": "KYC Analyst", "company": "Bank", "location": None, "url": "https://x",
         "first_seen": "2099-01-01T06:00:00+00:00", "score": 80, "notified": True, "salary_min": 9000,
         "salary_max": None, "salary_currency": None, "salary_period": None, "work_mode": None, "english_level": None,
         "one_line_reason": None, "strengths": ["Excel"], "gaps": [], "contract_types": None, "required_tools": [],
         "nice_to_have_tools": [], "role_category": "fraud/AML/KYC/compliance"},
        {"id": "b", "source": "linkedin", "title": "Data Analyst", "company": None, "location": "Kraków",
         "url": "https://y", "first_seen": "2099-01-01T05:00:00+00:00", "score": 70, "notified": True,
         "salary_min": 8000, "salary_max": 11000, "salary_currency": "PLN", "salary_period": "month",
         "work_mode": "hybrid", "english_level": "B2", "one_line_reason": "Good fit.", "strengths": [], "gaps": ["SQL"],
         "contract_types": ["B2B"], "required_tools": ["SQL"], "nice_to_have_tools": [], "role_category": "data/BI analytics"},
    ]

    def test_salary_and_tidy_with_missing_fields(self):
        import pandas as pd
        from data import SupabaseBackend
        with pd.option_context("future.infer_string", True):  # the default in pandas 3
            df = SupabaseBackend(FakeClient({"offers": self.OFFERS})).offers()
            self.assertIsNone(df.set_index("id").loc["a", "salary_period"])          # NaN -> None
        self.assertEqual(streamlit_app_salary(df.set_index("id").loc["a"]), "9 000 /mo")
        self.assertEqual(streamlit_app_salary(df.set_index("id").loc["b"]), "8 000–11 000 PLN/mo")

    def test_offers_page_renders_with_missing_fields(self):
        from data import SupabaseBackend
        from streamlit.testing.v1 import AppTest
        env = {k: v for k, v in os.environ.items() if k not in ("APP_BACKEND", "LOCAL_DB")}
        with mock.patch.dict(os.environ, env, clear=True):
            at = AppTest.from_file(str(APP), default_timeout=60)
            at.session_state["backend"] = SupabaseBackend(FakeClient({"offers": self.OFFERS}))
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any("2 offers" in c.value for c in at.caption))


def streamlit_app_salary(row):
    """Load just the salary helper from the app file without running the page."""
    import ast
    src = APP.read_text()
    tree = ast.parse(src)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("clean", "salary")]
    ns = {"pd": __import__("pandas")}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), "app", "exec"), ns)
    return ns["salary"](row)
