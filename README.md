# AI Job Scanner

A small automation that finds fresh job offers and scores them against my profile with an LLM, so every morning I only read the offers worth applying to.

**Pipeline:** LinkedIn public job search → title filter → Google Gemini (structured JSON scoring) → daily HTML e-mail
**Runs:** every weekday on GitHub Actions (free), no server needed.

## How it works
1. `scanner.py` pulls offers posted in the last 24 h for the searches in `config.yaml`.
2. Obvious mismatches (e.g. *senior*, *internship*) are dropped by title before any AI call, saving tokens.
3. Each remaining offer is sent to Gemini with a recruiter-style rubric (mandatory requirements 40, experience 20, education 15, languages 10, nice-to-haves 15). The response is forced into a JSON schema: score, verdict, strengths, gaps, one-line reason.
4. Offers above the threshold are e-mailed as an HTML digest, best first. Already-seen offers are cached, so nothing is scored twice.

## Privacy
The real candidate profile and all API keys live in GitHub Secrets. Logs print counts only, so the repo can stay public.

## Setup
1. Get a Gemini API key at https://aistudio.google.com/apikey
2. Create a Gmail app password (Google Account → Security → 2-Step Verification → App passwords)
3. Add repository secrets: `GEMINI_API_KEY`, `CANDIDATE_PROFILE`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD` (optional `EMAIL_TO`)
4. Actions → *Daily job scan* → *Run workflow* to test

Local test:
```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...
python scanner.py --dry-run
```

## Tech
Python · requests · BeautifulSoup · Google Gemini API (structured output) · SMTP e-mail · GitHub Actions (cron, secrets, cache)
