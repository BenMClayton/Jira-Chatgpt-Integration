# Jira OpenAI Report

A command-line reporting tool that retrieves Jira Cloud issues with cursor
pagination and asks an OpenAI model to turn them into a concise weekly status
report. Reports highlight overdue work, ownership gaps, risks, and next steps
while retaining the source issue list for traceability.

## Highlights

- Jira Cloud `POST /rest/api/3/search/jql` integration;
- retry and backoff handling for rate limits and temporary failures;
- optional date filters and JSON output;
- HTTPS enforcement before Jira credentials are transmitted; and
- explicit exit codes for automation.

## Setup

Requires Python 3.10 or newer.

```sh
python -m venv .venv
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with a Jira Cloud account, API token, project key, and OpenAI API
key. The example values are placeholders only.

## Run

```sh
python main.py
python main.py --project ABC --since 2025-01-01 --until 2025-12-31
python main.py --project ABC --json weekly_report.json
```

Run `python main.py --help` for all options. A sanitised output example is in
[`examples/weekly_report.example.txt`](examples/weekly_report.example.txt).

## Data handling

Issue summaries, status, assignee display names, due dates, and priorities are
sent to the configured OpenAI API model to create the report. Review your
organisation's Jira and AI data policies before using the tool. Generated
reports may also contain project information and are ignored by Git by default.

Never commit `.env`, API tokens, or reports generated from a real Jira project.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Report generated successfully |
| `2` | Missing or invalid configuration |
| `3` | Jira/OpenAI request failed after retries |

## Verification

```sh
python -m py_compile main.py
```
