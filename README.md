# AI Job Scanner

A small automation that finds fresh job offers and scores them against my profile with an LLM, so every morning I only read the offers worth applying to. Every scored offer is also stored, so over time the scanner builds a dataset of the job market I'm targeting.

**Pipeline:** LinkedIn + pracuj.pl + No Fluff Jobs + justjoin.it + RocketJobs → title filter → cross-portal de-duplication → Google Gemini (structured JSON: fit score + extracted facts) → SQLite → daily HTML e-mail + weekly market & skill-gap report (with CSV export for Power BI)
**Runs:** every weekday on GitHub Actions (free), no server needed.

## How it works
1. `scanner.py` pulls offers posted in the last 24 h for the searches in `config.yaml` from every portal listed under `sources` (one module per portal in `sources/`: LinkedIn's public job search, the JSON pracuj.pl embeds in its pages, the No Fluff Jobs API and the API shared by justjoin.it and its non-IT sister site RocketJobs). A portal that fails doesn't stop the others – the e-mail says which one broke.
   The same job is often listed on several portals; offers with the same normalised company + title (legal forms like *Sp. z o.o.* and gender tags like *(k/m)* removed) are scored only once. When the per-run cap is reached, portals share it round-robin.
2. Obvious mismatches (e.g. *senior*, *internship*) are dropped by title before any AI call, saving tokens. Optionally `include_title_keywords` keeps only titles containing one of the given words – useful because some portals match keywords loosely.
   At most `max_offers_per_run` offers are scored per run; the rest go to a **queue** in the database and are scored first on the next run (queued offers older than `queue_max_age_days` are dropped, failed ones are retried up to 3 times). The digest says how many offers are still waiting.
3. **AI pre-filter (triage):** new offers are sent to Gemini in batches of 60 titles (title, company, location, portal – one call per batch). Clear mismatches – a different profession, seniority far above the candidate, a language in the title they don't speak – are dropped before the expensive per-offer scoring; when unsure the model keeps the offer, and if a call fails the whole batch is kept. Dropped offers are stored and listed in the weekly report so the filter can be checked.
4. Each remaining offer is sent to Gemini in **one call** that returns a JSON object enforced by a schema:
   - **fit** – score from a recruiter-style rubric (mandatory requirements 40, experience 20, education 15, languages 10, nice-to-haves 15), verdict, strengths, gaps (as short reusable skill names), one-line reason;
   - **facts about the offer** – role category, industry, work mode, contract types, salary range (currency, period, gross/net), years required, English level, whether Polish is required, required and nice-to-have tools.
5. Salaries are normalised to **PLN per month** (`fx_to_pln` in `config.yaml`), so offers can be compared.
6. **Every** scored offer – not only the good ones – is saved to `data/jobs.db` (SQLite), together with a compressed copy of the description for later re-scoring/evaluation. Each run is logged too (searches, offers found, filtered, scored, failed).
7. Offers above the threshold are e-mailed as an HTML digest, best first. On Mondays (configurable) the full dataset is attached as `jobs.csv`.
8. Once a week (Monday by default) a **market & skill-gap report** is e-mailed – see below.
9. If the portals return 0 offers across all searches, the e-mail carries a warning – that usually means the page layout changed or the request was blocked, not that there are no jobs.

## Weekly market & skill-gap report
All numbers are computed in Python straight from the database; the LLM only receives the aggregates and turns them into at most three learning recommendations, so it cannot invent counts.

- headline numbers vs. the previous week (offers, average score, apply / stretch, near misses);
- **what blocks near-miss offers** – the gaps in offers that scored just below the e-mail threshold (default 40–59). These are the most actionable: closing them turns real offers into "apply";
- most common gaps overall with the week-over-week trend;
- most required tools, and in how many of those offers the tool was the candidate's gap;
- categories with average score and median salary (PLN/month), work-mode split, results per portal, best offers of the week;
- offers skipped by the AI pre-filter (count and the latest titles);
- up to 3 recommendations from Gemini (skill, why – citing the numbers, first step this week); skipped when fewer than 10 offers were scored.

Gap names are merged case-insensitively. On report days the CSV is attached to the report instead of the daily digest.

## Data model
| table | one row per | main columns |
|---|---|---|
| `offers` | scored offer | title, company, location, score, verdict, gaps, role_category, work_mode, salary_min/max_pln_month, required_tools, english_level, notified, user_label, status |
| `runs` | scanner run | started/finished, searches, cards found, dropped by title, scored, failed, notified, queued, model |
| `queue` | offer waiting to be scored | offer details, queued at, failed attempts |

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
Python · requests · BeautifulSoup · REST/JSON APIs · Google Gemini API (structured output) · SQLite (encrypted at rest) · SMTP e-mail · GitHub Actions (cron, secrets, cache)
