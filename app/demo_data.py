"""Build a demo database with synthetic offers, for local development and screenshots.
python app/demo_data.py data/demo.db  ->  APP_BACKEND=local LOCAL_DB=data/demo.db streamlit run app/streamlit_app.py
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import Store  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from data import LocalBackend  # noqa: E402

random.seed(11)
TITLES = [("KYC Analyst", "fraud/AML/KYC/compliance"), ("AML Analyst with German", "fraud/AML/KYC/compliance"),
          ("Junior Fraud Analyst", "fraud/AML/KYC/compliance"), ("Młodszy Specjalista ds. Weryfikacji Klientów", "fraud/AML/KYC/compliance"),
          ("Data Analyst (Power BI)", "data/BI analytics"), ("Reporting Specialist", "data/BI analytics"),
          ("Data Quality Analyst", "data/BI analytics"), ("Business Analyst", "business analysis"),
          ("Trust & Safety Specialist", "trust & safety/brand protection"), ("Brand Protection Analyst", "trust & safety/brand protection"),
          ("Risk Analyst", "risk"), ("Finance Operations Specialist", "finance/operations")]
COMPANIES = ["Allegro", "ING Hub Poland", "Revolut", "Amazon", "PwC", "Worldline", "Euroclear", "LSEG", "Santander",
             "Shell Business Operations", "State Street", "CERI International"]
CITIES = ["Warszawa", "Kraków", "Łódź", "Poznań", "Szczecin", "Remote"]
GAPS = ["SQL"] * 5 + ["German B2"] * 2 + ["Power BI (advanced)"] * 3 + ["Jira"] * 2 + ["SAP"] + ["3+ years in AML"] * 2 + ["Python"]
TOOLS = ["Excel", "SQL", "Power BI", "Jira", "Python", "SAP", "Tableau", "Confluence"]
SOURCES = ["linkedin"] * 4 + ["pracuj"] * 3 + ["justjoin"] * 2 + ["nofluffjobs", "rocketjobs"]


def parts(score: int) -> list[int]:
    """Split a demo score into the five parts (40/20/15/10/15) roughly in proportion."""
    caps = [40, 20, 15, 10, 15]
    out = [min(c, round(c * score / 100)) for c in caps]
    out[0] = max(0, min(40, out[0] + score - sum(out)))
    return out


def build(path: str) -> None:
    Path(path).unlink(missing_ok=True)
    st = Store(Path(path))
    now = datetime.now(timezone.utc)
    for i in range(260):
        title, cat = random.choice(TITLES)
        score = int(random.triangular(25, 92, 52))
        sal = random.choice([None, None, random.randrange(7000, 15000, 500)])
        st.save_offer({
            "id": f"demo-{i}", "source": random.choice(SOURCES), "title": title, "company": random.choice(COMPANIES),
            "location": random.choice(CITIES), "url": "https://example.com/offer",
            "first_seen": (now - timedelta(hours=random.uniform(0.5, 24 * 30))).isoformat(),
            "score": score, "verdict": "apply" if score >= 60 else ("stretch" if score >= 40 else "skip"),
            **dict(zip(["score_requirements", "score_experience", "score_education", "score_languages",
                        "score_extras"], parts(score))),
            "seniority": "junior", "one_line_reason": "Verification and reporting experience fits; SQL is the main gap.",
            "strengths": ["2 years of KYC verification", "Advanced Excel", "English C2"],
            "gaps": random.sample(GAPS, random.randint(1, 3)), "role_category": cat,
            "work_mode": random.choice(["hybrid"] * 3 + ["remote", "on-site"]), "contract_types": ["employment contract"],
            "salary_min": sal, "salary_max": sal and sal + 3000, "salary_currency": "PLN" if sal else None,
            "salary_period": "month" if sal else None, "salary_type": "gross" if sal else None,
            "salary_min_pln_month": sal, "salary_max_pln_month": sal and sal + 3000, "english_level": "B2",
            "required_tools": random.sample(TOOLS, 3), "notified": score >= 60,
        })
    for k in range(14):
        t = (now - timedelta(days=k, hours=3)).replace(microsecond=0)
        st.save_run({"started_at": t.isoformat(), "finished_at": (t + timedelta(minutes=random.randint(18, 38))).isoformat(),
                     "new_offers": random.randint(60, 140), "triaged_out": random.randint(30, 80),
                     "scored": random.randint(50, 150), "notified": random.randint(3, 20), "queued": random.choice([0, 0, 12]),
                     "gemini_errors": random.choice([0, 0, 0, 2]), "window_hours": 24,
                     "sources": json.dumps({"linkedin": {"cards": 600}, "pracuj": {"cards": 850}})})
    st.enqueue([{"id": f"q{i}", "title": t, "company": c, "source": "pracuj", "url": "https://example.com"}
                for i, (t, c) in enumerate([("Specjalista ds. AML", "mBank"), ("Fraud Prevention Analyst", "Allegro")])])
    st.save_triaged([{"id": f"t{i}", "title": t, "company": "X", "source": "pracuj", "url": "https://example.com"}
                     for i, t in enumerate(["Java Developer", "Księgowa", "Sales Manager", "Kierowca kat. C"])])
    st.save_skipped([{"id": "s1", "title": "Senior AML Investigator", "company": "Y", "source": "linkedin",
                      "url": "https://example.com"}], "title")
    st.close()
    app = LocalBackend(path)
    today = datetime.now().date()
    for oid, status, d_ago in (("demo-3", "applied", 9), ("demo-8", "interview", 14), ("demo-15", "applied", 3),
                               ("demo-21", "saved", None), ("demo-30", "rejected", 20)):
        applied = today - timedelta(days=d_ago) if d_ago else None
        app.save_application(oid, status=status, applied_on=applied,
                             follow_up_on=applied + timedelta(days=7) if applied else None,
                             notes="Sent CV via portal" if applied else None)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "data/demo.db")
    print("demo database ready")
