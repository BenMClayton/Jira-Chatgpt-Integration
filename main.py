# Jira Report Generator using OpenAI
# JiraOpenAIReport-1.2.0 (uses POST /rest/api/3/search/jql with cursor pagination)
# Python 3.10+ (tested 3.10/3.11/3.12)

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests import Response
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore


# ---------- Logging & Config ----------

def ensure_basic_logging(default_level: str = "INFO"):
    root = logging.getLogger()
    if not root.handlers:
        level = getattr(logging, default_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def ensure_config_variables_set(required_keys: Iterable[str]) -> None:
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        for key in missing:
            logging.error("Missing required environment variable: %s", key)
        sys.exit(2)


def enforce_https(url: str) -> str:
    if not url.lower().startswith("https://"):
        raise SystemExit("JIRA_BASE_URL must start with 'https://'. Refusing to send credentials over plain HTTP.")
    return url


# ---------- Model ----------

@dataclass
class JiraIssue:
    key: str
    summary: str
    status_name: str
    assignee_display_name: str
    due_date_raw: Optional[str]
    priority_name: Optional[str]

    @property
    def due_date_human(self) -> str:
        if not self.due_date_raw:
            return "No due date"
        try:
            parsed = datetime.strptime(self.due_date_raw, "%Y-%m-%d")
            return parsed.strftime("%d %b %Y")
        except Exception:
            return self.due_date_raw

    def as_line(self) -> str:
        priority_suffix = f" [Priority: {self.priority_name}]" if self.priority_name else ""
        return (
            f"{self.key}: {self.summary} — {self.status_name} "
            f"(Owner: {self.assignee_display_name}, Due: {self.due_date_human}){priority_suffix}"
        )


# ---------- Jira API (POST /search/jql) ----------

def build_jql(project_key: str, since: Optional[str], until: Optional[str]) -> str:
    clauses = [f"project = {project_key}"]
    if since:
        clauses.append(f"(created >= \"{since}\" OR updated >= \"{since}\")")
    if until:
        clauses.append(f"(created <= \"{until}\" OR updated <= \"{until}\")")
    return " AND ".join(clauses) + " ORDER BY duedate ASC"


def parse_jira_issues(payload: Dict[str, Any]) -> List[JiraIssue]:
    issues: List[JiraIssue] = []
    for issue in payload.get("issues", []) or []:
        fields = issue.get("fields", {}) or {}
        issues.append(
            JiraIssue(
                key=issue.get("key", "?"),
                summary=(fields.get("summary") or "(no summary)").strip(),
                status_name=(fields.get("status") or {}).get("name") or "Unknown",
                assignee_display_name=(fields.get("assignee") or {}).get("displayName") or "Unassigned",
                due_date_raw=fields.get("duedate"),
                priority_name=(fields.get("priority") or {}).get("name"),
            )
        )
    return issues


def _diagnostic_log(resp: Response) -> None:
    try:
        data = resp.json()
        msgs = data.get("errorMessages") or []
        errs = data.get("errors") or {}
        if msgs:
            logging.error("Jira errorMessages: %s", msgs)
        if errs:
            logging.error("Jira errors: %s", errs)
        if not msgs and not errs:
            logging.error("Jira response (JSON w/o error fields): %s", data)
    except Exception:
        logging.error("Jira non-JSON error body: %s", resp.text[:1000])


def _with_retries(desc: str, fn, max_retries: int) -> Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp: Response = fn()
            if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                if attempt <= max_retries:
                    wait = min(2 ** (attempt - 1), 30)
                    logging.warning("%s returned %s. Retry %s/%s in %ss...", desc, resp.status_code, attempt, max_retries, wait)
                    time.sleep(wait)
                    continue
                _diagnostic_log(resp)
                resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt <= max_retries:
                wait = min(2 ** (attempt - 1), 30)
                logging.warning("%s error (%s). Retry %s/%s in %ss...", desc, exc.__class__.__name__, attempt, max_retries, wait)
                time.sleep(wait)
                continue
            logging.error("%s failed after retries: %s", desc, exc)
            raise SystemExit(3)


def jira_search_cursor(
    base_url: str,
    email: str,
    api_token: str,
    jql: str,
    fields: List[str],
    max_results: int = 100,
    timeout: int = 30,
    max_retries: int = 5,
) -> List[JiraIssue]:
    """
    Jira Cloud new search endpoint:
      POST /rest/api/3/search/jql
    Cursor pagination via: nextPageToken (string) and isLast (bool).
    """
    url = base_url.rstrip("/") + "/rest/api/3/search/jql"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = HTTPBasicAuth(email, api_token)

    collected: List[JiraIssue] = []
    next_page_token: Optional[str] = None

    while True:
        body: Dict[str, Any] = {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        def do_post():
            return requests.post(url, headers=headers, json=body, auth=auth, timeout=timeout)

        resp = _with_retries("Jira POST /search/jql", do_post, max_retries)
        if resp.status_code >= 400:
            _diagnostic_log(resp)
        resp.raise_for_status()

        data = resp.json() or {}
        batch = parse_jira_issues(data)
        collected.extend(batch)

        is_last: bool = bool(data.get("isLast", True))
        next_page_token = data.get("nextPageToken")

        if is_last or not next_page_token:
            break

    return collected


# ---------- Report building ----------

def build_tasks_text_block(issues: List[JiraIssue]) -> str:
    if not issues:
        return "No issues were returned for the selected project."
    return "\n".join(i.as_line() for i in issues)


def generate_weekly_report(
    tasks_block: str,
    model_name: str,
    optional_system_prompt: Optional[str],
    max_retries: int = 3,
) -> str:
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK missing. Install with: pip install openai")

    client = OpenAI()

    system_message = (optional_system_prompt or "").strip()
    user_message = (
        "Here are the tasks pulled from Jira (one per line). "
        "Summarize progress, call out risks (overdue/blocked/unassigned), and propose concrete next steps.\n\n"
        f"TASKS:\n{tasks_block}"
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as exc:
            if attempt <= max_retries:
                wait = min(2 ** (attempt - 1), 20)
                logging.warning("OpenAI transient error. Retry %s/%s in %ss...", attempt, max_retries, wait)
                time.sleep(wait)
                continue
            logging.error("OpenAI API error: %s", exc)
            return (
                "Overall Status\n- Unable to generate AI summary.\n\n"
                "Key Risks & Blockers\n- Review Jira connectivity or OpenAI key.\n\n"
                "Next Steps\n- Re-run the script after fixing configuration."
            )


def write_text(path: str, content: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logging.info("Wrote text report: %s", path)
    except OSError as exc:
        logging.error("Failed writing %s: %s", path, exc)


def write_json(path: str, obj: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        logging.info("Wrote JSON: %s", path)
    except OSError as exc:
        logging.error("Failed writing %s: %s", path, exc)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate a weekly Jira status report using OpenAI.")
    p.add_argument("--project", dest="project_key", help="Jira project key (e.g., ABC). Overrides JIRA_PROJECT_KEY.")
    p.add_argument("--output", dest="output_txt", default=os.getenv("REPORT_OUTPUT", "weekly_report.txt"),
                   help="Path to write the text report (default: weekly_report.txt or REPORT_OUTPUT).")
    p.add_argument("--json", dest="output_json", default=os.getenv("REPORT_JSON_OUTPUT", ""),
                   help="Optional path to also write a JSON file of issues (default: none).")
    p.add_argument("--model", dest="model_name", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                   help="OpenAI Chat Completions model (default: gpt-4o-mini).")
    p.add_argument("--max-results", type=int, default=int(os.getenv("JIRA_MAX_RESULTS", "100")),
                   help="Max results per page when querying Jira (default: 100).")
    p.add_argument("--since", dest="since", default=os.getenv("JIRA_SINCE", ""),
                   help="Optional date filter (YYYY-MM-DD) for created/updated >= since.")
    p.add_argument("--until", dest="until", default=os.getenv("JIRA_UNTIL", ""),
                   help="Optional date filter (YYYY-MM-DD) for created/updated <= until.")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"),
                   help="Logging level (DEBUG, INFO, WARNING, ERROR).")
    return p.parse_args()


def main() -> int:
    if os.path.exists(".env"):
        load_dotenv(".env")

    args = parse_args()
    ensure_basic_logging(args.log_level)

    ensure_config_variables_set([
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "OPENAI_API_KEY",
    ])

    base_url = enforce_https(os.getenv("JIRA_BASE_URL", "").strip())
    email = os.getenv("JIRA_EMAIL", "").strip()
    api_token = os.getenv("JIRA_API_TOKEN", "").strip()
    project_key = (args.project_key or os.getenv("JIRA_PROJECT_KEY", "")).strip()
    if not project_key:
        logging.error("No project key provided. Use --project or set JIRA_PROJECT_KEY.")
        return 2

    model_name = args.model_name.strip()
    optional_system_prompt = os.getenv("AI_PROMPT")
    output_txt = args.output_txt.strip()
    output_json = args.output_json.strip()
    max_results = max(1, min(args.max_results, 100))
    since = args.since.strip() or None
    until = args.until.strip() or None

    logging.info("Building JQL for project '%s'...", project_key)
    jql = build_jql(project_key, since, until)

    logging.info("Fetching issues via POST /rest/api/3/search/jql (cursor pagination)...")
    issues: List[JiraIssue] = jira_search_cursor(
        base_url=base_url,
        email=email,
        api_token=api_token,
        jql=jql,
        fields=["summary", "status", "assignee", "duedate", "priority"],
        max_results=max_results,
    )

    if not issues:
        msg = f"No issues found for project '{project_key}' with current filters."
        logging.warning(msg)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        banner = f"Weekly Status Report for {project_key} — generated {timestamp}\n"
        divider = "=" * len(banner)
        final_text_output = f"\n{banner}{divider}\n\n{msg}\n"
        print(final_text_output)
        write_text(output_txt, final_text_output)
        if output_json:
            write_json(output_json, {"project": project_key, "issues": [], "jql": jql})
        return 0

    tasks_block = build_tasks_text_block(issues)
    logging.info("Generating weekly report with OpenAI model '%s'...", model_name)
    ai_report_text = generate_weekly_report(
        tasks_block=tasks_block,
        model_name=model_name,
        optional_system_prompt=optional_system_prompt,
    )

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    banner = f"Weekly Status Report for {project_key} — generated {timestamp}\n"
    divider = "=" * len(banner)
    final_text_output = f"\n{banner}{divider}\n\n{ai_report_text}\n\n---\nRaw Tasks\n{tasks_block}\n"

    print(final_text_output)
    write_text(output_txt, final_text_output)
    if output_json:
        write_json(output_json, {"project": project_key, "issues": [asdict(i) for i in issues], "jql": jql})

    return 0


if __name__ == "__main__":
    sys.exit(main())
