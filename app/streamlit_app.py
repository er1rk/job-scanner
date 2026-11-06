"""AI Job Scanner - web app (Streamlit).

Live:  secrets SUPABASE_URL, SUPABASE_ANON_KEY (+ GITHUB_TOKEN, GITHUB_REPO for "Scan now").
Local: APP_BACKEND=local LOCAL_DB=data/jobs.db streamlit run app/streamlit_app.py  (no login)
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from data import STATUSES, LocalBackend, SupabaseBackend  # noqa: E402

st.set_page_config(page_title="AI Job Scanner", page_icon="🔎", layout="wide")

PORTALS = {"linkedin": "LinkedIn", "pracuj": "pracuj.pl", "nofluffjobs": "No Fluff Jobs",
           "justjoin": "justjoin.it", "rocketjobs": "RocketJobs"}
# fixed categorical order, so a portal keeps its colour whatever the filter (validated palette)
PORTAL_COLORS = {"LinkedIn": "#2a78d6", "pracuj.pl": "#eb6834", "No Fluff Jobs": "#1baf7a",
                 "justjoin.it": "#eda100", "RocketJobs": "#e87ba4"}
ACCENT, MUTED = "#2a78d6", "#8a8984"
REASONS = {"ai": "AI pre-filter", "title": "Title filter", "duplicate": "Duplicate"}


def secret(name: str, default: str | None = None) -> str | None:
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:  # no secrets file (local run)
        return os.getenv(name, default)


# ---------------------------------------------------------------- backend + login
def get_backend():
    if os.getenv("APP_BACKEND") == "local":
        if "backend" not in st.session_state:
            st.session_state.backend = LocalBackend(os.getenv("LOCAL_DB", "data/jobs.db"))
        return st.session_state.backend
    if "backend" in st.session_state:
        return st.session_state.backend
    from supabase import create_client
    url, key = secret("SUPABASE_URL"), secret("SUPABASE_ANON_KEY")
    if not (url and key):
        st.error("Set SUPABASE_URL and SUPABASE_ANON_KEY in the app's secrets.")
        st.stop()
    st.title("🔎 AI Job Scanner")
    with st.form("login"):
        email = st.text_input("E-mail")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary"):
            client = create_client(url, key)
            try:
                client.auth.sign_in_with_password({"email": email, "password": password})
            except Exception:
                st.error("Wrong e-mail or password.")
                st.stop()
            st.session_state.backend = SupabaseBackend(client)
            st.rerun()
    st.stop()


def load(backend, force: bool = False) -> dict:
    if force or "data" not in st.session_state:
        st.session_state.data = {
            "offers": backend.offers(), "runs": backend.runs(), "queue": backend.queue(),
            "skipped": backend.skipped(), "apps": backend.applications(), "settings": backend.settings(),
            "effective": backend.effective_settings(),
            "loaded": datetime.now(timezone.utc),
        }
    return st.session_state.data


# ---------------------------------------------------------------- helpers
def salary(r) -> str:
    lo, hi = r.get("salary_min"), r.get("salary_max")
    if pd.isna(lo) and pd.isna(hi):
        return ""
    lo = hi if pd.isna(lo) else lo
    hi = lo if pd.isna(hi) else hi
    rng = f"{lo:,.0f}" if lo == hi else f"{lo:,.0f}–{hi:,.0f}"
    return f"{rng.replace(',', ' ')} {r.get('salary_currency') or ''}/{(r.get('salary_period') or 'month')[:2]}".strip()


def current(d: dict) -> dict:
    """Settings in force: the scanner's last effective settings, overridden by what was saved in the app."""
    return {**(d.get("effective") or {}), **(d.get("settings") or {})}


def with_status(offers: pd.DataFrame, apps: pd.DataFrame) -> pd.DataFrame:
    df = offers.copy()
    status = dict(zip(apps["offer_id"], apps["status"])) if not apps.empty else {}
    df["status"] = df["id"].map(status).fillna("")
    df["portal"] = df["source"].map(PORTALS).fillna(df["source"])
    df["salary"] = df.apply(salary, axis=1)
    return df


def dispatch_scan(inputs: dict) -> tuple[bool, str]:
    token, repo = secret("GITHUB_TOKEN"), secret("GITHUB_REPO", "er1rk/job-scanner")
    if not token:
        return False, "Add GITHUB_TOKEN to the app's secrets to start scans from here."
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/main.yml/dispatches",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"},
                      json={"ref": "main", "inputs": inputs}, timeout=20)
    if r.status_code == 204:
        return True, "Scan started on GitHub Actions – results arrive by e-mail and here in ~30 minutes."
    return False, f"GitHub answered {r.status_code}: {r.text[:150]}"


def bar(df: pd.DataFrame, x: str, y: str, title: str, height: int = 260):
    return (alt.Chart(df, title=title, height=height)
            .mark_bar(color=ACCENT, cornerRadiusEnd=4, size=14)
            .encode(x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(grid=True, tickMinStep=1)),
                    y=alt.Y(f"{y}:N", sort="-x", title=None),
                    tooltip=[alt.Tooltip(f"{y}:N", title=""), alt.Tooltip(f"{x}:Q", title="offers")])
            .configure_view(stroke=None).configure_axis(labelColor="#52514e", gridColor="#eceae6", domain=False))


def explode_count(series: pd.Series, n: int = 12) -> pd.DataFrame:
    items = series.explode().dropna()
    items = items[items.astype(str).str.strip() != ""]
    norm = items.astype(str).str.replace(r"^(?i:ms|microsoft)\s+", "", regex=True).str.strip()
    out = norm.str.lower().value_counts().head(n).reset_index()
    out.columns = ["key", "count"]
    label = norm.groupby(norm.str.lower()).agg(lambda s: s.value_counts().index[0])
    out["label"] = out["key"].map(label)
    return out


# ---------------------------------------------------------------- pages
def page_offers(backend, d):
    if d["offers"].empty:
        st.info("No offers yet – they appear after the next scan syncs to the app.")
        return
    offers = with_status(d["offers"], d["apps"])
    threshold = int(current(d).get("min_score_to_notify", 60))
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    query = f1.text_input("Search", placeholder="title, company or skill…", label_visibility="collapsed")
    days = f2.selectbox("Period", [1, 3, 7, 30, 3650], index=2,
                        format_func=lambda v: "all time" if v > 365 else f"last {v} day{'s' if v > 1 else ''}",
                        label_visibility="collapsed")
    min_score = f3.slider("Min score", 0, 100, threshold, step=5, label_visibility="collapsed",
                          help="Minimum score")
    hide = f4.toggle("Hide applied / rejected", value=True)
    with st.expander("More filters"):
        g1, g2, g3 = st.columns(3)
        portals = g1.multiselect("Portal", sorted(offers["portal"].dropna().unique()))
        cats = g2.multiselect("Category", sorted(offers["role_category"].dropna().unique()))
        modes = g3.multiselect("Work mode", sorted(offers["work_mode"].dropna().unique()))

    df = offers[offers["first_seen"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
    df = df[df["score"].fillna(0) >= min_score]
    if query:
        hay = (df["title"].fillna("") + " " + df["company"].fillna("") + " " +
               df["required_tools"].apply(lambda v: " ".join(v)) + " " + df["gaps"].apply(lambda v: " ".join(v)))
        df = df[hay.str.contains(query, case=False, regex=False)]
    if hide:
        df = df[~df["status"].isin(["applied", "interview", "offer", "rejected", "not interested"])]
    for col, sel in (("portal", portals), ("role_category", cats), ("work_mode", modes)):
        if sel:
            df = df[df[col].isin(sel)]
    df = df.sort_values(["score", "first_seen"], ascending=False)

    st.caption(f"{len(df)} offers · sorted by score")
    table = df[["score", "title", "company", "location", "portal", "salary", "status", "first_seen", "url"]]
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        height=min(38 * len(table) + 40, 520),
        column_config={
            "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d", width="small"),
            "title": st.column_config.TextColumn("Title", width="medium"),
            "first_seen": st.column_config.DatetimeColumn("Found", format="D MMM, HH:mm", width="small"),
            "url": st.column_config.LinkColumn("Link", display_text="Open ↗", width="small"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "company": st.column_config.TextColumn("Company", width="medium"),
            "location": st.column_config.TextColumn("Location", width="small"),
            "portal": st.column_config.TextColumn("Portal", width="small"),
            "salary": st.column_config.TextColumn("Salary", width="small"),
        })
    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.caption("Select a row to see the details and track your application.")
        return
    offer_detail(backend, df.iloc[rows[0]], d)


def offer_detail(backend, o, d):
    with st.container(border=True):
        a, b = st.columns([5, 1])
        a.subheader(o["title"])
        a.markdown(f"{o['company']} · {o['location']} · {o['portal']} · [open offer ↗]({o['url']})")
        b.metric("Score", int(o["score"]) if pd.notna(o["score"]) else "–")
        facts = [x for x in (o["salary"], o.get("work_mode"), ", ".join(o.get("contract_types") or []),
                             f"EN {o['english_level']}" if o.get("english_level") not in (None, "unknown") else "")
                 if x and x != "unknown"]
        if facts:
            st.markdown(" · ".join(f"`{x}`" for x in facts))
        st.write(o.get("one_line_reason") or "")
        c1, c2 = st.columns(2)
        c1.markdown("**Strengths**\n" + "\n".join(f"- ✓ {x}" for x in o["strengths"]) if o["strengths"] else "")
        c2.markdown("**Gaps**\n" + "\n".join(f"- ✗ {x}" for x in o["gaps"]) if o["gaps"] else "")
        if o.get("required_tools"):
            st.caption("Required tools: " + ", ".join(o["required_tools"]))

        apps = d["apps"]
        cur = apps[apps["offer_id"] == o["id"]].iloc[0].to_dict() if not apps.empty and (apps["offer_id"] == o["id"]).any() else {}
        with st.form(f"track-{o['id']}"):
            s1, s2, s3 = st.columns(3)
            status = s1.selectbox("Status", STATUSES, index=STATUSES.index(cur["status"]) if cur.get("status") in STATUSES else 0)
            applied = s2.date_input("Applied on", value=pd.to_datetime(cur.get("applied_on")).date()
                                    if cur.get("applied_on") else None)
            follow = s3.date_input("Follow up on", value=pd.to_datetime(cur.get("follow_up_on")).date()
                                   if cur.get("follow_up_on") else None)
            notes = st.text_area("Notes", value=cur.get("notes") or "", height=80)
            if st.form_submit_button("Save", type="primary"):
                if status == "applied" and not applied:
                    applied = date.today()
                if status == "applied" and not follow:
                    follow = (applied or date.today()) + timedelta(days=7)  # default reminder a week later
                backend.save_application(o["id"], status=status, applied_on=applied and applied.isoformat(),
                                         follow_up_on=follow and follow.isoformat(), notes=notes or None)
                d["apps"] = backend.applications()
                st.toast("Saved")
                st.rerun()


def page_applications(backend, d):
    apps = d["apps"]
    if apps.empty:
        st.info("No applications tracked yet – pick an offer in **Offers** and set its status.")
        return
    offers = d["offers"][["id", "title", "company", "score", "url"]].rename(columns={"id": "offer_id"})
    df = apps.merge(offers, on="offer_id", how="left")
    df["applied_on"] = pd.to_datetime(df["applied_on"], errors="coerce").dt.date
    df["follow_up_on"] = pd.to_datetime(df["follow_up_on"], errors="coerce").dt.date
    df["notes"] = df["notes"].fillna("")
    df[["title", "company"]] = df[["title", "company"]].fillna("(offer no longer in the database)")

    counts = df["status"].value_counts()
    cols = st.columns(len(STATUSES))
    for c, s in zip(cols, STATUSES):
        c.metric(s.capitalize(), int(counts.get(s, 0)))

    follow_ts = pd.to_datetime(df["follow_up_on"], errors="coerce")  # works even when every date is empty
    due = df[(df["status"].isin(["applied", "interview"])) & (follow_ts <= pd.Timestamp(date.today()))]
    if not due.empty:
        st.warning("**Follow-ups due:** " + "; ".join(f"{r.title} ({r.company})" for r in due.itertuples()))

    order = {s: i for i, s in enumerate(["interview", "offer", "applied", "saved", "rejected", "not interested"])}
    df = df.sort_values(by=["status", "follow_up_on"], key=lambda c: c.map(order) if c.name == "status" else c)
    edited = st.data_editor(
        df[["status", "title", "company", "score", "applied_on", "follow_up_on", "notes", "url", "offer_id"]],
        hide_index=True, width="stretch", disabled=["title", "company", "score", "url", "offer_id"],
        column_order=["status", "title", "company", "score", "applied_on", "follow_up_on", "notes", "url"],
        column_config={
            "status": st.column_config.SelectboxColumn("Status", options=STATUSES, required=True, width="small"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "applied_on": st.column_config.DateColumn("Applied"),
            "follow_up_on": st.column_config.DateColumn("Follow up"),
            "notes": st.column_config.TextColumn("Notes", width="medium"),
            "url": st.column_config.LinkColumn("Link", display_text="Open ↗", width="small"),
            "score": st.column_config.NumberColumn("Score", format="%d", width="small"),
            "company": st.column_config.TextColumn("Company"),
        }, key="apps_editor")
    if st.button("Save changes", type="primary"):
        before = df.set_index("offer_id")
        changed = 0
        for r in edited.itertuples():
            b = before.loc[r.offer_id]
            same = lambda x, y: (pd.isna(x) and pd.isna(y)) or x == y
            if not all(same(x, y) for x, y in ((r.status, b.status), (r.applied_on, b.applied_on),
                                                (r.follow_up_on, b.follow_up_on), (r.notes, b.notes))):
                backend.save_application(r.offer_id, status=r.status,
                                         applied_on=r.applied_on.isoformat() if pd.notna(r.applied_on) and r.applied_on else None,
                                         follow_up_on=r.follow_up_on.isoformat() if pd.notna(r.follow_up_on) and r.follow_up_on else None,
                                         notes=r.notes if isinstance(r.notes, str) and r.notes else None)
                changed += 1
        d["apps"] = backend.applications()
        st.toast(f"Saved {changed} change(s)")
        st.rerun()


def page_scanner(backend, d):
    runs, queue, skipped = d["runs"], d["queue"], d["skipped"]
    last = runs.iloc[0] if not runs.empty else None
    m1, m2, m3, m4 = st.columns(4)
    if last is not None:
        ago = (pd.Timestamp.now(tz="UTC") - last["finished_at"]).total_seconds() / 3600
        m1.metric("Last scan", f"{ago:.0f} h ago" if ago >= 1 else "just now",
                  help=last["finished_at"].tz_convert("Europe/Warsaw").strftime("%d %b %H:%M"))
        m2.metric("Scored in last scan", int(last.get("scored") or 0))
        m3.metric("Gemini errors", int(last.get("gemini_errors") or 0))
    m4.metric("Waiting in queue", len(queue))

    with st.container(border=True):
        st.markdown("**Run the scanner**")
        b1, b2 = st.columns(2)
        if b1.button("🔄 Scan now", type="primary", width="stretch"):
            ok, msg = dispatch_scan({"hours": "24"})
            (st.success if ok else st.error)(msg)
        if b2.button("📊 Send weekly report now", width="stretch"):
            ok, msg = dispatch_scan({"report_only": "true"})
            (st.success if ok else st.error)(msg.replace("Scan started", "Report requested").split(" – ")[0])

    st.markdown("#### Search settings")
    st.caption("Pre-filled with what the last scan used. Changes are saved in the app and used from the next scan on.")
    s = current(d)
    with st.form("settings"):
        c1, c2 = st.columns(2)
        kw = c1.text_area("Keywords (one per line)", "\n".join(s.get("keywords", [])), height=200)
        loc = c2.text_area("Locations (one per line, e.g. Warsaw, Poland)", "\n".join(s.get("locations", [])), height=200)
        c3, c4 = st.columns(2)
        inc = c3.text_area("Title must contain one of", "\n".join(s.get("include_title_keywords", [])), height=140)
        exc = c4.text_area("Skip titles with (whole words, * = stem)", "\n".join(s.get("exclude_title_keywords", [])), height=140)
        score = st.number_input("Minimum score for the e-mail", 0, 100, int(s.get("min_score_to_notify", 60)), step=5)
        if st.form_submit_button("Save settings", type="primary"):
            lines = lambda t: [x.strip() for x in t.splitlines() if x.strip()]
            for key, val in (("keywords", lines(kw)), ("locations", lines(loc)), ("include_title_keywords", lines(inc)),
                             ("exclude_title_keywords", lines(exc)), ("min_score_to_notify", int(score))):
                backend.save_setting(key, val)
            d["settings"] = backend.settings()
            st.toast("Settings saved – used from the next scan")

    st.markdown("#### Recent scans")
    if not runs.empty:
        t = runs.head(14).copy()
        t["when"] = t["started_at"].dt.tz_convert("Europe/Warsaw")
        t["minutes"] = ((t["finished_at"] - t["started_at"]).dt.total_seconds() / 60).round()
        st.dataframe(t[["when", "minutes", "new_offers", "triaged_out", "scored", "notified", "queued", "gemini_errors"]],
                     hide_index=True, width="stretch",
                     column_config={"when": st.column_config.DatetimeColumn("Started", format="D MMM, HH:mm"),
                                    "new_offers": "New", "triaged_out": "AI skipped", "scored": "Scored",
                                    "notified": "E-mailed", "queued": "Queue", "gemini_errors": "Gemini errors",
                                    "minutes": "Minutes"})

    st.markdown("#### Skipped offers (last 30 days)")
    if skipped.empty:
        st.caption("Nothing skipped yet.")
    else:
        q1, q2 = st.columns([3, 2])
        q = q1.text_input("Find a skipped offer", placeholder="title or company", label_visibility="collapsed")
        why = q2.multiselect("Reason", list(REASONS.values()), default=["AI pre-filter"], label_visibility="collapsed")
        sk = skipped.assign(reason=skipped["reason"].map(REASONS).fillna(skipped["reason"]),
                            portal=skipped["source"].map(PORTALS))
        if why:
            sk = sk[sk["reason"].isin(why)]
        if q:
            sk = sk[(sk["title"].fillna("") + " " + sk["company"].fillna("")).str.contains(q, case=False, regex=False)]
        st.dataframe(sk[["reason", "title", "company", "portal", "url"]].head(300), hide_index=True,
                     width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="Open ↗")})
    if not queue.empty:
        with st.expander(f"Queue ({len(queue)})"):
            st.dataframe(queue[["title", "company", "source", "url"]], hide_index=True, width="stretch",
                         column_config={"url": st.column_config.LinkColumn("Link", display_text="Open ↗")})


def page_market(d):
    offers = d["offers"]
    if offers.empty:
        st.info("No data yet.")
        return
    days = st.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda v: f"{v} days") or 30
    df = offers[offers["first_seen"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)].copy()
    threshold = int(current(d).get("min_score_to_notify", 60))
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Offers scored", len(df))
    k2.metric("Average score", f"{df['score'].mean():.0f}" if len(df) else "–")
    k3.metric(f"≥ {threshold} (e-mailed)", int((df["score"] >= threshold).sum()))
    near = df[(df["score"] >= threshold - 20) & (df["score"] < threshold)]
    k4.metric("Near misses", len(near), help=f"score {threshold - 20}–{threshold - 1}")

    c1, c2 = st.columns(2)
    gaps = explode_count(near["gaps"] if len(near) else df["gaps"])
    if not gaps.empty:
        c1.altair_chart(bar(gaps, "count", "label", "What blocks near-miss offers"), width="stretch")
    tools = explode_count(df["required_tools"])
    if not tools.empty:
        c2.altair_chart(bar(tools, "count", "label", "Most required tools"), width="stretch")

    daily = (df.assign(day=df["first_seen"].dt.tz_convert("Europe/Warsaw").dt.date,
                       portal=df["source"].map(PORTALS))
             .groupby(["day", "portal"]).size().reset_index(name="offers"))
    if not daily.empty:
        # parts of a daily total -> stacked bars (fixed colour per portal, so filters never repaint them)
        order = [p for p in PORTAL_COLORS if p in set(daily["portal"])]
        stacked = (alt.Chart(daily, title="Offers scored per day, by portal", height=260)
                   .mark_bar(stroke="#fcfcfb", strokeWidth=1)
                   .encode(x=alt.X("day:T", title=None, axis=alt.Axis(format="%d %b", labelAngle=0)),
                           y=alt.Y("sum(offers):Q", title=None),
                           color=alt.Color("portal:N", scale=alt.Scale(domain=order,
                                                                        range=[PORTAL_COLORS[p] for p in order]),
                                           sort=order, legend=alt.Legend(orient="top", title=None)),
                           order=alt.Order("portal_rank:Q"),
                           tooltip=[alt.Tooltip("day:T", format="%a %d %b"), "portal:N", "offers:Q"])
                   .transform_calculate(portal_rank="indexof(" + str(order) + ", datum.portal)")
                   .configure_view(stroke=None).configure_axis(labelColor="#52514e", gridColor="#eceae6", domain=False))
        st.altair_chart(stacked, width="stretch")

    sal = df.dropna(subset=["salary_min_pln_month", "salary_max_pln_month"])
    if len(sal):
        g = (sal.groupby("role_category")
             .agg(offers=("id", "size"), low=("salary_min_pln_month", "median"), high=("salary_max_pln_month", "median"))
             .reset_index().sort_values("high", ascending=False))
        st.markdown("**Median salary range by category** (PLN / month, offers that state both ends)")
        g[["low", "high"]] = g[["low", "high"]].round(-2)
        st.dataframe(g, hide_index=True, width="stretch",
                     column_config={"role_category": "Category", "offers": "Offers",
                                    "low": st.column_config.NumberColumn("From", format="%d"),
                                    "high": st.column_config.NumberColumn("To", format="%d")})


# ---------------------------------------------------------------- main
backend = get_backend()
with st.sidebar:
    st.markdown("### 🔎 AI Job Scanner")
    refresh = st.button("↻ Refresh data", width="stretch")
d = load(backend, force=refresh)
with st.sidebar:
    st.caption(f"Data loaded {d['loaded'].astimezone().strftime('%H:%M')} · {len(d['offers'])} offers")
    if os.getenv("APP_BACKEND") != "local" and st.button("Log out", width="stretch"):
        st.session_state.clear()
        st.rerun()

t1, t2, t3, t4 = st.tabs(["Offers", "My applications", "Scanner", "Market"])
with t1:
    page_offers(backend, d)
with t2:
    page_applications(backend, d)
with t3:
    page_scanner(backend, d)
with t4:
    page_market(d)
