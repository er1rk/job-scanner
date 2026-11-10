"""AI Job Scanner - web app (Streamlit).

Live:  secrets SUPABASE_URL, SUPABASE_ANON_KEY (+ GITHUB_TOKEN, GITHUB_REPO for "Scan now").
Local: APP_BACKEND=local LOCAL_DB=data/jobs.db streamlit run app/streamlit_app.py  (no login)
"""

from __future__ import annotations

import hashlib
import html
import os
import re
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
           "justjoin": "justjoin.it", "rocketjobs": "RocketJobs", "web": "Other site"}
# fixed categorical order, so a portal keeps its colour whatever the filter (validated palette)
PORTAL_COLORS = {"LinkedIn": "#2a78d6", "pracuj.pl": "#eb6834", "No Fluff Jobs": "#1baf7a",
                 "justjoin.it": "#eda100", "RocketJobs": "#e87ba4", "Other site": "#8a8984"}
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


def load_feedback(backend):
    """None = the feedback table doesn't exist yet (supabase/schema.sql not re-run) - the app still works."""
    try:
        st.session_state.pop("feedback_error", None)
        return backend.feedback()
    except Exception as e:
        st.session_state.feedback_error = f"{type(e).__name__}: {str(e)[:200]}"
        return None


def load(backend, force: bool = False) -> dict:
    if force or "data" not in st.session_state:
        st.session_state.data = {
            "offers": backend.offers(), "runs": backend.runs(), "queue": backend.queue(),
            "skipped": backend.skipped(), "apps": backend.applications(), "settings": backend.settings(),
            "effective": backend.effective_settings(), "feedback": load_feedback(backend),
            "loaded": datetime.now(timezone.utc),
        }
    return st.session_state.data


# ---------------------------------------------------------------- helpers
def clean(v):
    """None for any kind of missing value (None, NaN, NA) - lists and dicts are kept as they are."""
    if isinstance(v, (list, dict)):
        return v
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def salary(r) -> str:
    lo, hi = clean(r.get("salary_min")), clean(r.get("salary_max"))
    if lo is None and hi is None:
        return ""
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    try:
        rng = f"{float(lo):,.0f}" if lo == hi else f"{float(lo):,.0f}–{float(hi):,.0f}"
    except (TypeError, ValueError):
        return ""
    cur = clean(r.get("salary_currency")) or ""
    per = str(clean(r.get("salary_period")) or "month")[:2]
    return f"{rng.replace(',', ' ')} {cur}/{per}".replace("  ", " ").strip()


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


# ---------------------------------------------------------------- look & feel
CSS = """
<style>
.block-container {padding-top: 4.2rem; max-width: 1240px;}
.jc-label {font-size: 12px; font-weight: 600; margin-bottom: 1px;}
.jc-title {font-size: 15px; font-weight: 600; line-height: 1.3; color: #1f1f1e;}
.jc-meta {font-size: 13px; color: #52514e; margin-top: 2px;}
.jc-chip {display: inline-block; font-size: 12px; color: #52514e; background: #f1efeb; border-radius: 10px;
          padding: 2px 9px; margin: 6px 4px 0 0; white-space: nowrap;}
.jc-status {display: inline-block; font-size: 11px; font-weight: 600; border-radius: 10px; padding: 2px 8px;
            background: #e7f1fb; color: #1d5fae; margin-left: 6px; vertical-align: 2px;}
.jc-ring {width: 44px; height: 44px; line-height: 38px; border-radius: 22px; border: 3px solid; text-align: center;
          font-size: 16px; font-weight: 700; float: right; margin-left: 10px;}
.jc-ring.big {width: 58px; height: 58px; line-height: 52px; border-radius: 29px; font-size: 20px;}
.jc-reason {font-size: 14px; line-height: 1.5; color: #1f1f1e; margin: 10px 0 6px;}
.jc-list div {font-size: 13px; line-height: 1.45; color: #52514e; padding-left: 16px; text-indent: -16px; margin: 2px 0;}
.jc-parts {margin: 4px 0 10px;}
.jc-part {display: flex; align-items: center; gap: 10px; font-size: 12px; color: #52514e; margin: 3px 0;}
.jc-part span {width: 92px; flex: none;}
.jc-part b {width: 38px; text-align: right; font-weight: 600; color: #1f1f1e; flex: none;}
.jc-bar {flex: 1; height: 6px; background: #ecebe7; border-radius: 3px; overflow: hidden;}
.jc-bar div {height: 100%; border-radius: 3px;}
.jc-kanban-head {font-size: 13px; font-weight: 600; color: #52514e; text-transform: uppercase; letter-spacing: .03em;}
.jc-due {color: #b42318; font-weight: 600;}
div[class*="st-key-sel-"] {border-color: #2a78d6 !important; box-shadow: 0 0 0 1px #2a78d6;}
/* desktop: the offer details stay on screen while the list scrolls under them */
@media (min-width: 641px) {
  .st-key-offers-layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {align-items: flex-start;}
  .st-key-offers-layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:last-child {
    position: sticky; top: 4.2rem; max-height: calc(100vh - 5rem); overflow-y: auto; z-index: 1;}
}
@media (max-width: 640px) {
  .block-container {padding-left: 1rem; padding-right: 1rem; padding-top: 3.6rem;}
  /* phone: show the selected offer's details ABOVE the list */
  .st-key-offers-layout div[data-testid="stHorizontalBlock"] {flex-direction: column-reverse;}
  .st-key-offers-layout div[data-testid="stHorizontalBlock"] div[data-testid="stHorizontalBlock"] {flex-direction: column;}
  /* phone: summary numbers as a 2 x 2 grid instead of four rows */
  .st-key-kpis div[data-testid="stHorizontalBlock"] {flex-direction: row; flex-wrap: wrap; gap: .5rem 0;}
  .st-key-kpis div[data-testid="stColumn"] {min-width: 50% !important; flex: 1 1 50% !important;}
  .st-key-kpis div[data-testid="stMetricValue"] {font-size: 1.6rem;}
  .jc-ring {width: 38px; height: 38px; line-height: 32px; font-size: 14px;}
}
</style>
"""
GOOD, AMBER, LOW = "#1a7f37", "#9a6700", "#8a8984"
STATUS_LABEL = {"saved": "Saved", "applied": "Applied", "interview": "Interview", "offer": "Offer",
                "rejected": "Rejected", "not interested": "Not interested"}


def tier(score, threshold):
    s = clean(score) or 0
    if s >= max(75, threshold):
        return GOOD, "Strong match"
    if s >= threshold:
        return AMBER, "Worth a look"
    return LOW, "Below threshold"


def card_html(o, threshold: int, big: bool = False) -> str:
    """Offer card body in the style of the e-mail digest (title, meta, score ring, fact chips)."""
    col, label = tier(o.get("score"), threshold)
    esc = lambda v: html.escape(str(v)) if clean(v) is not None else ""
    status = clean(o.get("status"))
    badge = f"<span class='jc-status'>{STATUS_LABEL.get(status, status)}</span>" if status else ""
    meta = " · ".join(esc(x) for x in (o.get("company"), o.get("location"), o.get("portal"), ago(o.get("first_seen")))
                      if clean(x))
    en = clean(o.get("english_level"))
    chips = [o.get("salary"), clean(o.get("work_mode")) if clean(o.get("work_mode")) != "unknown" else None,
             ", ".join(clean(o.get("contract_types")) or []) or None,
             f"EN {en}" if en not in (None, "unknown", "none") else None]
    chips_html = "".join(f"<span class='jc-chip'>{html.escape(c)}</span>" for c in chips if c)
    score = int(clean(o.get("score")) or 0)
    return (f"<div class='jc-ring{' big' if big else ''}' style='border-color:{col};color:{col}'>{score}</div>"
            f"<div class='jc-label' style='color:{col}'>{label}{badge}</div>"
            f"<div class='jc-title' style='font-size:{19 if big else 15}px'>{esc(o.get('title'))}</div>"
            f"<div class='jc-meta'>{meta}</div><div>{chips_html}</div>")


def set_status(backend, d, offer_id: str, status: str, **extra) -> None:
    fields = {"status": status, **extra}
    if status == "applied":
        today = date.today()
        fields.setdefault("applied_on", today.isoformat())
        fields.setdefault("follow_up_on", (today + timedelta(days=7)).isoformat())  # reminder a week later
    backend.save_application(offer_id, **fields)
    d["apps"] = backend.applications()


def quick_actions(backend, d, o, key: str) -> None:
    """Save / Applied / Not interested in one tap."""
    status = clean(o.get("status"))
    a, b, c = st.columns(3)
    if a.button("★ Save", key=f"save-{key}", width="stretch", disabled=status == "saved"):
        set_status(backend, d, o["id"], "saved")
        st.toast("Saved for later")
        st.rerun()
    if b.button("✓ Applied", key=f"applied-{key}", width="stretch", type="primary", disabled=status == "applied"):
        set_status(backend, d, o["id"], "applied")
        st.toast("Marked as applied – follow-up reminder in 7 days")
        st.rerun()
    if c.button("✕ Not interested", key=f"no-{key}", width="stretch", disabled=status == "not interested"):
        set_status(backend, d, o["id"], "not interested")
        st.toast("Hidden from the list")
        st.rerun()


# ---------------------------------------------------------------- pages
def page_home():
    backend, d = APP["backend"], APP["data"]
    offers = with_status(d["offers"], d["apps"]) if not d["offers"].empty else d["offers"]
    threshold = int(current(d).get("min_score_to_notify", 60))
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=24)
    fresh = offers[offers["first_seen"] >= since] if not offers.empty else offers
    matches = fresh[fresh["score"].fillna(0) >= threshold] if not fresh.empty else fresh
    apps = d["apps"]
    follow = pd.to_datetime(apps["follow_up_on"], errors="coerce") if not apps.empty else pd.Series(dtype="datetime64[ns]")
    due = apps[(apps["status"].isin(["applied", "interview"])) & (follow <= pd.Timestamp(date.today()))] if not apps.empty else apps
    runs = d["runs"]
    last = runs.iloc[0] if not runs.empty else None

    st.markdown(f"### {datetime.now().strftime('%A')}, {date.today().day} {datetime.now().strftime('%B')}")
    k1, k2, k3, k4 = st.container(key="kpis").columns(4)
    k1.metric("New offers (24 h)", len(fresh))
    k2.metric(f"New matches ≥ {threshold}", len(matches))
    k3.metric("Follow-ups due", len(due))
    if last is not None and pd.notna(last["finished_at"]):
        hours = (pd.Timestamp.now(tz="UTC") - last["finished_at"]).total_seconds() / 3600
        ok = hours < 26 and not (clean(last.get("gemini_errors")) or 0)
        k4.metric("Scanner", f"{'✓' if ok else '⚠'} {hours:.0f} h ago" if hours >= 1 else "✓ just now",
                  help=f"Last scan: {int(clean(last.get('scored')) or 0)} scored, "
                       f"{int(clean(last.get('queued')) or 0)} queued, "
                       f"{int(clean(last.get('gemini_errors')) or 0)} Gemini errors")
        if hours >= 26:
            st.warning("No scan in the last day – check the cron trigger or start one in **Scanner**.")
    else:
        k4.metric("Scanner", "no scans yet")

    if not due.empty:
        st.markdown("#### Follow-ups due")
        names = offers.set_index("id") if not offers.empty else pd.DataFrame()
        for r in due.itertuples():
            o = names.loc[r.offer_id] if r.offer_id in names.index else {}
            with st.container(border=True):
                t, b1, b2 = st.columns([6, 2, 2], vertical_alignment="center")
                applied = clean(r.applied_on)
                t.markdown(f"**{html.escape(str(clean(o.get('title')) or r.offer_id))}** · {html.escape(str(clean(o.get('company')) or ''))}"
                           f"  \n<span class='jc-meta'>{STATUS_LABEL.get(r.status)} {applied or ''} · follow-up "
                           f"<span class='jc-due'>{clean(r.follow_up_on)}</span></span>", unsafe_allow_html=True)
                if b1.button("Followed up +7 d", key=f"fu-{r.offer_id}", width="stretch"):
                    set_status(backend, d, r.offer_id, r.status,
                               follow_up_on=(date.today() + timedelta(days=7)).isoformat())
                    st.rerun()
                if b2.button("Rejected", key=f"rj-{r.offer_id}", width="stretch"):
                    set_status(backend, d, r.offer_id, "rejected")
                    st.rerun()

    st.markdown("#### Best new matches")
    top = matches[~matches["status"].isin(["applied", "interview", "offer", "rejected", "not interested"])] \
        if not matches.empty else matches
    if top.empty:
        st.caption("Nothing new above the threshold in the last 24 hours.")
    for _, o in top.sort_values("score", ascending=False).head(5).iterrows():
        with st.container(border=True):
            st.markdown(card_html(o, threshold), unsafe_allow_html=True)
            st.markdown(f"<div class='jc-reason'>{html.escape(str(clean(o.get('one_line_reason')) or ''))}</div>",
                        unsafe_allow_html=True)
            quick_actions(backend, d, o, f"home-{o['id']}")
            l1, l2 = st.columns(2)
            if l1.button("Details →", key=f"det-{o['id']}", width="stretch"):
                st.session_state.sel = o["id"]
                st.switch_page(PAGES["offers"])
            if clean(o.get("url")):
                l2.link_button("Open offer ↗", o["url"], width="stretch")


def score_one_offer():
    """Paste a link from any job site -> the scanner scores it (whatever the filters say) and e-mails the result."""
    with st.expander("➕ Score an offer from a link"):
        with st.form("score-one", border=False, clear_on_submit=True):
            c1, c2 = st.columns([5, 2], vertical_alignment="bottom")
            url = c1.text_input("Link to the offer", placeholder="https://… (LinkedIn, pracuj.pl, a company career page…)")
            go = c2.form_submit_button("Score it", type="primary", width="stretch")
        if go:
            url = url.strip()
            if not re.match(r"https?://\S+\.\S+", url):
                st.error("Paste the full link, starting with https://")
            else:
                ok, msg = dispatch_scan({"score_url": url})
                if ok:
                    st.success("Scoring started – the result comes by e-mail in about 2 minutes and appears in this "
                               "list after **↻ Refresh data**.")
                else:
                    st.error(msg)


SORTS = {"Best match first": (["score", "first_seen"], "best first"),
         "Newest first": (["first_seen", "score"], "newest first"),
         "Highest salary first": (["salary_max_pln_month", "score"], "highest salary first")}


def ago(ts) -> str:
    """'3 h ago' / '2 d ago' for when the scanner found the offer."""
    if ts is None or pd.isna(ts):
        return ""
    h = (pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 3600
    return "just now" if h < 1 else f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def page_offers():
    backend, d = APP["backend"], APP["data"]
    if d["offers"].empty:
        st.info("No offers yet – they appear after the next scan syncs to the app.")
        return
    offers = with_status(d["offers"], d["apps"])
    threshold = int(current(d).get("min_score_to_notify", 60))
    score_one_offer()
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    query = f1.text_input("Search", placeholder="Search title, company or skill…", label_visibility="collapsed")
    days = f2.selectbox("Period", [1, 3, 7, 30, 3650], index=2, label_visibility="collapsed",
                        format_func=lambda v: "All time" if v > 365 else f"Last {v} day{'s' if v > 1 else ''}")
    min_score = f3.slider("Min score", 0, 100, threshold, step=5, label_visibility="collapsed",
                          help="Minimum score")
    order = f4.selectbox("Sort", list(SORTS), key="offers-sort", label_visibility="collapsed")
    with st.expander("More filters"):
        g1, g2, g3, g4 = st.columns(4)
        portals = g1.multiselect("Portal", sorted(offers["portal"].dropna().unique()))
        cats = g2.multiselect("Category", sorted(offers["role_category"].dropna().unique()))
        modes = g3.multiselect("Work mode", sorted(offers["work_mode"].dropna().unique()))
        hide = g4.toggle("Hide applied / rejected / not interested", value=True)

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
    cols, caption = SORTS[order]
    df = df.sort_values(cols, ascending=False, na_position="last")

    sel_id = st.session_state.get("sel")
    with st.container(key="offers-layout"):
        left, right = st.columns([5, 6], gap="medium")
        with left:
            shown = st.session_state.get("shown", 20)
            st.caption(f"{len(df)} offers · {caption}")
            for _, o in df.head(shown).iterrows():
                key = ("sel-" if o["id"] == sel_id else "card-") + hashlib.md5(str(o["id"]).encode()).hexdigest()[:10]
                with st.container(border=True, key=key):
                    st.markdown(card_html(o, threshold), unsafe_allow_html=True)
                    if st.button("Details", key=f"open-{o['id']}", width="stretch",
                                 type="secondary" if o["id"] != sel_id else "primary"):
                        st.session_state.sel = o["id"]
                        st.rerun()
            if len(df) > shown and st.button(f"Show more ({len(df) - shown} left)", width="stretch"):
                st.session_state.shown = shown + 20
                st.rerun()
        with right:
            pick = offers[offers["id"] == sel_id]
            if pick.empty:
                st.caption("Pick an offer to see why it scored what it did and to track it.")
            else:
                offer_detail(backend, pick.iloc[0], d, threshold)


SCORE_PARTS = [("score_requirements", "Requirements", 40), ("score_experience", "Experience", 20),
               ("score_education", "Education", 15), ("score_languages", "Languages", 10),
               ("score_extras", "Nice-to-haves", 15)]


def breakdown_html(o) -> str:
    """Where the points came from - one thin bar per part (offers scored before the change have none)."""
    rows = []
    for key, label, cap in SCORE_PARTS:
        v = clean(o.get(key))
        if v is None:
            return ""
        pct = max(0, min(100, round(100 * float(v) / cap)))
        col = GOOD if pct >= 75 else AMBER if pct >= 45 else "#b42318"
        rows.append(f"<div class='jc-part'><span>{label}</span><div class='jc-bar'><div style='width:{pct}%;"
                    f"background:{col}'></div></div><b>{int(v)}/{cap}</b></div>")
    return "<div class='jc-parts'>" + "".join(rows) + "</div>"


def cv_section(backend, o) -> None:
    """Generate a CV tailored to this offer (GitHub Actions + Gemini) and download the latest one."""
    try:
        made = backend.cv(o["id"])
    except Exception:  # cvs table not created yet
        made = None
    c1, c2 = st.columns(2)
    if c1.button("📄 Generate CV" if not made else "📄 New CV", key=f"cv-{o['id']}", width="stretch"):
        ok, msg = dispatch_scan({"cv_offer": str(o["id"])})
        if ok:
            st.success("Generating – the CV comes by e-mail in about 2 minutes and appears here after "
                       "**↻ Refresh data**.")
        else:
            st.error(msg)
    if made:
        import base64
        c2.download_button("⬇ Download CV", base64.b64decode(made["pdf_base64"]), file_name=made["file_name"],
                           mime="application/pdf", key=f"cvd-{o['id']}", width="stretch")
        when = pd.to_datetime(made.get("created_at"), utc=True)
        with st.expander(f"Form answers · CV from {when.tz_convert('Europe/Warsaw'):%d %b %H:%M}"
                         if pd.notna(when) else "Form answers"):
            for a in made.get("answers") or []:
                st.markdown(f"**{a.get('question', '')}**")
                st.code(a.get("answer", ""), language=None, wrap_lines=True)


def offer_detail(backend, o, d, threshold: int):
    with st.container(border=True):
        st.markdown(card_html(o, threshold, big=True), unsafe_allow_html=True)
        st.markdown(f"<div class='jc-reason'>{html.escape(str(clean(o.get('one_line_reason')) or ''))}</div>",
                    unsafe_allow_html=True)
        st.markdown(breakdown_html(o), unsafe_allow_html=True)
        plus = "".join(f"<div><span style='color:{GOOD}'>✓</span> {html.escape(x)}</div>" for x in o["strengths"])
        minus = "".join(f"<div><span style='color:#b42318'>✗</span> {html.escape(x)}</div>" for x in o["gaps"])
        c1, c2 = st.columns(2)
        c1.markdown(f"<div class='jc-list'>{plus}</div>", unsafe_allow_html=True)
        c2.markdown(f"<div class='jc-list'>{minus}</div>", unsafe_allow_html=True)
        tools = clean(o.get("required_tools")) or []
        if tools:
            st.caption("Required tools: " + ", ".join(tools))
        found = o["first_seen"].tz_convert("Europe/Warsaw").strftime("%d %b, %H:%M") if pd.notna(o["first_seen"]) else ""
        st.caption(f"Found {found} · search “{clean(o.get('search_keyword')) or '–'}”")
        quick_actions(backend, d, o, f"detail-{o['id']}")
        if clean(o.get("url")):
            st.link_button("Open offer ↗", o["url"], width="stretch")
        cv_section(backend, o)

        apps = d["apps"]
        mine = apps[apps["offer_id"] == o["id"]]
        cur = mine.iloc[0].to_dict() if not mine.empty else {}
        with st.expander("Interview, dates and notes"):
            with st.form(f"track-{o['id']}", border=False):
                status = st.selectbox("Status", STATUSES, format_func=STATUS_LABEL.get,
                                      index=STATUSES.index(cur["status"]) if cur.get("status") in STATUSES else 0)
                s2, s3 = st.columns(2)
                applied = s2.date_input("Applied on", value=pd.to_datetime(clean(cur.get("applied_on"))).date()
                                        if clean(cur.get("applied_on")) else None)
                follow = s3.date_input("Follow up on", value=pd.to_datetime(clean(cur.get("follow_up_on"))).date()
                                       if clean(cur.get("follow_up_on")) else None)
                notes = st.text_area("Notes", value=clean(cur.get("notes")) or "", height=90)
                if st.form_submit_button("Save", type="primary"):
                    set_status(backend, d, o["id"], status, applied_on=applied and applied.isoformat(),
                               follow_up_on=follow and follow.isoformat(), notes=notes or None)
                    st.toast("Saved")
                    st.rerun()


def page_applications():
    backend, d = APP["backend"], APP["data"]
    apps = d["apps"]
    if apps.empty:
        st.info("No applications tracked yet – use **★ Save** or **✓ Applied** on an offer.")
        return
    offers = d["offers"]
    info = offers.set_index("id") if not offers.empty else pd.DataFrame()
    follow = pd.to_datetime(apps["follow_up_on"], errors="coerce")
    columns = [("Saved", ["saved"], "applied"), ("Applied", ["applied"], "interview"),
               ("Interview", ["interview"], "offer"), ("Offer", ["offer"], None),
               ("Closed", ["rejected", "not interested"], None)]
    cols = st.columns(len(columns), gap="small")
    for col, (name, statuses, nxt) in zip(cols, columns):
        mask = apps["status"].isin(statuses)
        part = apps.loc[follow[mask].sort_values(na_position="last").index]  # nearest follow-up first
        with col:
            st.markdown(f"<div class='jc-kanban-head'>{name} · {len(part)}</div>", unsafe_allow_html=True)
            for r in part.itertuples():
                o = info.loc[r.offer_id] if r.offer_id in info.index else pd.Series(dtype=object)
                title = clean(o.get("title")) or "(offer not synced yet)"
                fu = clean(r.follow_up_on)
                is_due = fu and r.status in ("applied", "interview") and pd.to_datetime(fu).date() <= date.today()
                with st.container(border=True):
                    st.markdown(
                        f"<div class='jc-title' style='font-size:14px'>{html.escape(str(title))}</div>"
                        f"<div class='jc-meta'>{html.escape(str(clean(o.get('company')) or ''))}"
                        + (f" · {int(o['score'])}" if clean(o.get('score')) is not None else "") + "</div>"
                        + (f"<div class='jc-meta'>Applied {clean(r.applied_on)}</div>" if clean(r.applied_on) else "")
                        + (f"<div class='jc-meta'>Follow up <span class='{'jc-due' if is_due else ''}'>{fu}</span></div>" if fu else "")
                        + (f"<div class='jc-meta'>{STATUS_LABEL.get(r.status)}</div>" if name == "Closed" else ""),
                        unsafe_allow_html=True)
                    if nxt and st.button(f"→ {STATUS_LABEL[nxt]}", key=f"nx-{r.offer_id}", width="stretch"):
                        set_status(backend, d, r.offer_id, nxt)
                        st.rerun()
                    with st.popover("Edit", width="stretch"):
                        with st.form(f"kb-{r.offer_id}", border=False):
                            stt = st.selectbox("Status", STATUSES, index=STATUSES.index(r.status) if r.status in STATUSES else 0, format_func=STATUS_LABEL.get)
                            ap = st.date_input("Applied on", value=pd.to_datetime(clean(r.applied_on)).date() if clean(r.applied_on) else None)
                            fo = st.date_input("Follow up on", value=pd.to_datetime(fu).date() if fu else None)
                            nt = st.text_area("Notes", value=clean(r.notes) or "")
                            if st.form_submit_button("Save", type="primary"):
                                set_status(backend, d, r.offer_id, stt, applied_on=ap and ap.isoformat(),
                                           follow_up_on=fo and fo.isoformat(), notes=nt or None)
                                st.rerun()
                        if clean(o.get("url")):
                            st.link_button("Open offer ↗", o["url"], width="stretch")


def page_scanner():
    backend, d = APP["backend"], APP["data"]
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
        st.markdown("**Weekly report**")
        st.caption("Scans start from **🔄 Scan now** in the side panel.")
        if st.button("📊 Send weekly report now", width="stretch"):
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
        st.markdown("**For generated CVs** – used in the form answers; leave empty to get a placeholder")
        c5, c6 = st.columns(2)
        salary = c5.text_input("Salary expectation", s.get("cv_salary", ""), placeholder="e.g. 8 000 PLN gross / month")
        avail = c6.text_input("Availability / notice period", s.get("cv_availability", ""),
                              placeholder="e.g. available immediately")
        if st.form_submit_button("Save settings", type="primary"):
            lines = lambda t: [x.strip() for x in t.splitlines() if x.strip()]
            for key, val in (("keywords", lines(kw)), ("locations", lines(loc)), ("include_title_keywords", lines(inc)),
                             ("exclude_title_keywords", lines(exc)), ("min_score_to_notify", int(score)),
                             ("cv_salary", salary.strip()), ("cv_availability", avail.strip())):
                backend.save_setting(key, val)
            d["settings"] = backend.settings()
            st.toast("Settings saved – used from the next scan")

    st.markdown("#### Recent scans")
    if not runs.empty:
        t = runs.head(14).copy()
        t["when"] = t["started_at"].dt.tz_convert("Europe/Warsaw")
        t["minutes"] = ((t["finished_at"] - t["started_at"]).dt.total_seconds() / 60).round()
        st.dataframe(t[["when", "new_offers", "scored", "notified", "queued", "gemini_errors", "triaged_out", "minutes"]],
                     hide_index=True, width="stretch",
                     column_config={"when": st.column_config.DatetimeColumn("Started", format="D MMM, HH:mm"),
                                    "new_offers": "New", "triaged_out": "AI skipped", "scored": "Scored",
                                    "notified": "E-mailed", "queued": "Queue", "gemini_errors": "Gemini errors",
                                    "minutes": "Minutes"})

    st.markdown("#### Skipped offers (last 30 days)")
    fb = d.get("feedback")
    if fb is None:  # not loaded or the table was missing at login - check again instead of waiting for a refresh
        fb = d["feedback"] = load_feedback(backend)
    if skipped.empty:
        st.caption("Nothing skipped yet.")
    else:
        st.caption("Tick offers you'd want to see and press **★ Interested**: the next scan scores them, and the AI "
                   "pre-filter gets your marked titles as examples of offers to keep.")
        q1, q2 = st.columns([3, 2])
        q = q1.text_input("Find a skipped offer", placeholder="title or company", label_visibility="collapsed")
        why = q2.multiselect("Reason", list(REASONS.values()), default=["AI pre-filter"], label_visibility="collapsed")
        marked = set(fb["id"]) if fb is not None and not fb.empty else set()
        sk = skipped.assign(reason_label=skipped["reason"].map(REASONS).fillna(skipped["reason"]),
                            portal=skipped["source"].map(PORTALS),
                            interested=skipped["id"].isin(marked).map({True: "★", False: ""}))
        if why:
            sk = sk[sk["reason_label"].isin(why)]
        if q:
            sk = sk[(sk["title"].fillna("") + " " + sk["company"].fillna("")).str.contains(q, case=False, regex=False)]
        sk = sk.head(300).reset_index(drop=True)
        event = st.dataframe(sk[["interested", "title", "company", "url", "reason_label", "portal"]], hide_index=True,
                             width="stretch", on_select="rerun", selection_mode="multi-row", key="skipped-table",
                             column_config={"interested": st.column_config.TextColumn("★", width=36),
                                            "url": st.column_config.LinkColumn("Link", display_text="Open ↗"),
                                            "reason_label": "Skipped by", "title": "Title", "company": "Company",
                                            "portal": "Portal"})
        picked = sk.iloc[event.selection.rows] if event and event.selection.rows else sk.iloc[0:0]
        b1, b2 = st.columns(2)
        if fb is None:
            st.info("To mark offers, run the updated `supabase/schema.sql` once in Supabase (SQL Editor).")
            if st.session_state.get("feedback_error"):
                st.caption(f"Supabase said: {st.session_state.feedback_error}")
        else:
            if b1.button(f"★ Interested ({len(picked)})", type="primary", width="stretch", disabled=picked.empty):
                rows = [r for r in picked.to_dict("records") if r["reason"] != "duplicate"]
                if rows:
                    backend.mark_interested(rows)
                    d["feedback"] = load_feedback(backend)
                    st.toast(f"Marked {len(rows)} – scored on the next scan")
                notes = []
                if len(rows) < len(picked):
                    notes.append("Duplicates were left out – the same job was already scored from another portal.")
                if any(r["reason"] == "title" for r in rows):
                    notes.append("Some were dropped by the **title filter** – it only learns from the *Skip titles "
                                 "with* list above, so check which word matched.")
                st.session_state.skip_notes = notes
                st.rerun()
            if b2.button("Remove mark", width="stretch", disabled=picked.empty or not set(picked["id"]) & marked):
                backend.unmark_interested([i for i in picked["id"] if i in marked])
                d["feedback"] = load_feedback(backend)
                st.rerun()
        for note in st.session_state.pop("skip_notes", []):
            st.info(note)
        if marked:
            st.caption(f"★ {len(marked)} marked as interested so far – used by the AI pre-filter on every scan.")
    if not queue.empty:
        with st.expander(f"Queue ({len(queue)})"):
            st.dataframe(queue[["title", "company", "source", "url"]], hide_index=True, width="stretch",
                         column_config={"url": st.column_config.LinkColumn("Link", display_text="Open ↗")})


def page_market():
    d = APP["data"]
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
        # one bar per calendar day (a time axis put several ticks on each day and made the bars hair-thin)
        first = max(daily["day"].min(), date.today() - timedelta(days=days - 1))
        all_days = [first + timedelta(days=i) for i in range((date.today() - first).days + 1)]
        labels = [x.strftime("%d %b") for x in all_days]
        daily = daily[daily["day"] >= first]
        daily = daily.assign(label=daily["day"].map(lambda x: x.strftime("%d %b")),
                             weekday=daily["day"].map(lambda x: x.strftime("%a %d %b")))
        stacked = (alt.Chart(daily, title="Offers scored per day, by portal", height=260)
                   .mark_bar(stroke="#fcfcfb", strokeWidth=1)
                   .encode(x=alt.X("label:O", title=None, scale=alt.Scale(domain=labels, paddingInner=0.25),
                                   axis=alt.Axis(labelAngle=0, labelOverlap="greedy", ticks=False)),
                           y=alt.Y("sum(offers):Q", title=None),
                           color=alt.Color("portal:N", scale=alt.Scale(domain=order,
                                                                        range=[PORTAL_COLORS[p] for p in order]),
                                           sort=order, legend=alt.Legend(orient="top", title=None)),
                           order=alt.Order("portal_rank:Q"),
                           tooltip=[alt.Tooltip("weekday:N", title="day"), "portal:N", "offers:Q"])
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
st.markdown(CSS, unsafe_allow_html=True)
APP = {"backend": get_backend()}
refresh = st.session_state.pop("refresh", False)
APP["data"] = load(APP["backend"], force=refresh)


def sidebar_status(d: dict) -> None:
    """Always-visible scanner status + actions (desktop; on a phone it sits behind the ≫ icon)."""
    st.markdown("### 🔎 AI Job Scanner")
    runs, apps = d["runs"], d["apps"]
    last = runs.iloc[0] if not runs.empty else None
    if last is not None and pd.notna(last["finished_at"]):
        hours = (pd.Timestamp.now(tz="UTC") - last["finished_at"]).total_seconds() / 3600
        errors = int(clean(last.get("gemini_errors")) or 0)
        when = last["finished_at"].tz_convert("Europe/Warsaw").strftime("%d %b, %H:%M")
        ago = f"{hours:.0f} h ago" if hours >= 1 else "just now"
        if hours >= 26:
            st.warning(f"No scan for {ago.removesuffix(' ago')} (last {when})", icon=":material/warning:")
        elif errors:
            st.warning(f"Last scan {ago} · {errors} Gemini errors", icon=":material/warning:")
        else:
            st.success(f"Last scan {ago} · {when}", icon=":material/check_circle:")
        st.caption(f"Scored {int(clean(last.get('scored')) or 0)} · e-mailed {int(clean(last.get('notified')) or 0)}")
    else:
        st.info("No scans yet")
    follow = pd.to_datetime(apps["follow_up_on"], errors="coerce") if not apps.empty else pd.Series(dtype="datetime64[ns]")
    due = int(((apps["status"].isin(["applied", "interview"])) & (follow <= pd.Timestamp(date.today()))).sum()) \
        if not apps.empty else 0
    c1, c2 = st.columns(2)
    c1.metric("In queue", len(d["queue"]))
    c2.metric("Follow-ups due", due)
    if st.button("🔄 Scan now", type="primary", width="stretch", key="side-scan"):
        ok, msg = dispatch_scan({"hours": "24"})
        (st.success if ok else st.error)(msg)
    if st.button("↻ Refresh data", width="stretch", key="side-refresh"):
        st.session_state.refresh = True
        st.rerun()
    st.caption(f"Data loaded {d['loaded'].astimezone().strftime('%H:%M')} · {len(d['offers'])} offers")
    if os.getenv("APP_BACKEND") != "local" and st.button("Log out", width="stretch", key="side-logout"):
        st.session_state.clear()
        st.rerun()


with st.sidebar:
    sidebar_status(APP["data"])

PAGES = {
    "home": st.Page(page_home, title="Home", icon=":material/home:", url_path="home",
                    default=os.getenv("APP_START_PAGE", "home") == "home"),
    "offers": st.Page(page_offers, title="Offers", icon=":material/work:", url_path="offers",
                      default=os.getenv("APP_START_PAGE") == "offers"),
    "applications": st.Page(page_applications, title="Applications", icon=":material/view_kanban:",
                            url_path="applications", default=os.getenv("APP_START_PAGE") == "applications"),
    "scanner": st.Page(page_scanner, title="Scanner", icon=":material/radar:", url_path="scanner",
                       default=os.getenv("APP_START_PAGE") == "scanner"),
    "market": st.Page(page_market, title="Market", icon=":material/bar_chart:", url_path="market",
                      default=os.getenv("APP_START_PAGE") == "market"),
}
st.navigation(list(PAGES.values()), position="top").run()
