# AI Job Scanner

A small automation that finds fresh job offers and scores them against my profile with an LLM, so every morning I only read the offers worth applying to. Every scored offer is also stored, so over time the scanner builds a dataset of the job market I'm targeting.

**Pipeline:** LinkedIn public job search → title filter → Google Gemini (structured JSON: fit score + extracted facts) → SQLite → daily HTML e-mail + weekly market & skill-gap report (with CSV export for Power BI)
**Runs:** every weekday on GitHub Actions (free), no server needed.

## How it works
1. `scanner.py` pulls offers posted in the last 24 h for the searches in `config.yaml`.
2. Obvious mismatches (e.g. *senior*, *internship*) are dropped by title before any AI call, saving tokens.
3. Each remaining offer is sent to Gemini in **one call** that returns a JSON object enforced by a schema:
   - **fit** – score from a recruiter-style rubric (mandatory requirements 40, experience 20, education 15, languages 10, nice-to-haves 15), verdict, strengths, gaps (as short reusable skill names), one-line reason;
   - **facts about the offer** – role category, industry, work mode, contract types, salary range (currency, period, gross/net), years required, English level, whether Polish is required, required and nice-to-have tools.
4. Salaries are normalised to **PLN per month** (`fx_to_pln` in `config.yaml`), so offers can be compared.
5. **Every** scored offer – not only the good ones – is saved to `data/jobs.db` (SQLite), together with a compressed copy of the description for later re-scoring/evaluation. Each run is logged too (searches, offers found, filtered, scored, failed).
6. Offers above the threshold are e-mailed as an HTML digest, best first. On Mondays (configurable) the full dataset is attached as `jobs.csv`.
7. Once a week (Monday by default) a **market & skill-gap report** is e-mailed – see below.
8. If LinkedIn returns 0 offers across all searches, the e-mail carries a warning – that usually means the page layout changed or the request was blocked, not that there are no jobs.

## Weekly market & skill-gap report
All numbers are computed in Python straight from the database; the LLM only receives the aggregates and turns them into at most three learning recommendations, so it cannot invent counts.

- headline numbers vs. the previous week (offers, average score, apply / stretch, near misses);
- **what blocks near-miss offers** – the gaps in offers that scored just below the e-mail threshold (default 40–59). These are the most actionable: closing them turns real offers into "apply";
- most common gaps overall with the week-over-week trend;
- most required tools, and in how many of those offers the tool was the candidate's gap;
- categories with average score and median salary (PLN/month), work-mode split, best offers of the week;
- up to 3 recommendations from Gemini (skill, why – citing the numbers, first step this week); skipped when fewer than 10 offers were scored.

Gap names are merged case-insensitively. On report days the CSV is attached to the report instead of the daily digest.

## Data model
| table | one row per | main columns |
|---|---|---|
| `offers` | scored offer | title, company, location, score, verdict, gaps, role_category, work_mode, salary_min/max_pln_month, required_tools, english_level, notified, user_label, status |
| `runs` | scanner run | started/finished, searches, cards found, dropped by title, scored, failed, notified, model |

List fields (gaps, tools, contract types) are exported to CSV joined with ` | ` – in Power Query use *Split column → By delimiter → Rows* to count e.g. the most common gaps.

## Privacy
The repo is public; everything personal stays out of it:
- **Candidate profile, API keys and search settings** live in GitHub Secrets. `config.yaml` holds only neutral public defaults; the real keywords, locations, exclusions and role categories come from the `PRIVATE_CONFIG` secret (or a git-ignored `config.private.yaml` locally) and replace those keys.
- **The database** is kept in the GitHub Actions cache, never in the repo, and only **encrypted** (Fernet, key in the `DB_ENCRYPTION_KEY` secret). A run in Actions without the key stops instead of caching plaintext.
- **Reports and digests** go only to the configured e-mail address.
- **Logs** print counts only – no offer titles, gap names or config values.

## Setup
1. Get a Gemini API key at https://aistudio.google.com/apikey
2. Create a Gmail app password (Google Account → Security → 2-Step Verification → App passwords)
3. Add repository secrets: `GEMINI_API_KEY`, `CANDIDATE_PROFILE`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `DB_ENCRYPTION_KEY` (a long random passphrase – keep a copy, without it the stored data can't be read), `PRIVATE_CONFIG` (your search settings in the format of `config.private.example.yaml`); optional `EMAIL_TO`
4. Actions → *Daily job scan* → *Run workflow* to test (tick *Attach the full dataset* / *Send the weekly report* to get them right away)

Local use:
```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...
python scanner.py --dry-run     # prints results, saves and sends nothing
python scanner.py --export      # data/jobs.db -> data/jobs.csv
python scanner.py --report      # weekly report -> data/report.html (no e-mail)
python -m unittest -v           # offline tests (no network, no API key)
```

## Tech
Python · requests · BeautifulSoup · Google Gemini API (structured output) · SQLite (encrypted at rest) · SMTP e-mail · GitHub Actions (cron, secrets, cache)
