# JiraOpenAIReport 1.1.0

## Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values

## Run
python weekly_report.py --project ABC --json weekly_report.json --since 2025-01-01 --until 2025-12-31
# or: python weekly_report.py  (if JIRA_PROJECT_KEY is in .env)

## Exit codes
0 success; 2 config error; 3 network/API failure (after retries)