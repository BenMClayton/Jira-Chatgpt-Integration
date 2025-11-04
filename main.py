# Jira Report Generator using OpenAI
# JiraOpenAIReport-1.0.0
# Made By Ben Clayton
# Made for Python 3.12
# Install Requirements with: pip install -r requirements.txt

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests import Response
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

from openai import OpenAI


def ensure_basic_logging(default_level: str = "INFO"):
    # Make INFO/DEBUG logs visible when no logging is configured elsewhere
    root = logging.getLogger()
    if not root.handlers:
        level = getattr(logging, default_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def ensure_config_variables_set(required_keys: Iterable[str]):
    # Exit the program with a clear error if any required environment variables are missing.
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        for key in missing:
            logging.error("Missing required environment variable: %s", key)
        sys.exit(1)


@dataclass
class JiraIssue:
    # Minimal, readable representation of a Jira issue for reporting.
    key: str
    summary: str
    status_name: str
    assignee_display_name: str
    due_date_raw: Optional[str]
    priority_name: Optional[str]

    @property
    def due_date_human(self) -> str:
        # Convert YYYY-MM-DD into a friendlier 'DD Mon YYYY' string.
        if not self.due_date_raw:
            return "No due date"
        try:
            parsed = datetime.strptime(self.due_date_raw, "%Y-%m-%d")
            return parsed.strftime("%d %b %Y")
        except Exception:
            # If Jira returns an unexpected format, keep the raw value for transparency.
            return self.due_date_raw


def build_jira_request_body(project_key: str, next_page_token: Optional[str]) -> Dict[str, Any]:
    # Create the POST body for Jira search (/rest/api/3/search/jql).
    body: Dict[str, Any] = {
        "jql": f"project = {project_key} ORDER BY duedate ASC",
        "fields": ["summary", "status", "assignee", "duedate", "priority"],
        "maxResults": 100,
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return body


def parse_jira_issues(payload: Dict[str, Any]) -> List[JiraIssue]:
    # Convert Jira API's JSON payload into a list of JiraIssue objects.
    parsed: List[JiraIssue] = []
    for issue in payload.get("issues", []) or []:
        fields = issue.get("fields", {}) or {}
        parsed.append(
            JiraIssue(
                key=issue.get("key", "?"),
                summary=(fields.get("summary") or "(no summary)").strip(),
                status_name=(fields.get("status") or {}).get("name") or "Unknown",
                assignee_display_name=(fields.get("assignee") or {}).get("displayName") or "Unassigned",
                due_date_raw=fields.get("duedate"),
                priority_name=(fields.get("priority") or {}).get("name"),
            )
        )
    return parsed


def fetch_all_jira_issues( base_url: str, email: str, api_token: str, project_key: str, http_timeout_seconds: int = 30) -> List[JiraIssue]:
    # Fetch all issues for a project using Jira's POST /rest/api/3/search/jql endpoint,
    # following nextPageToken pagination until all results are retrieved.
    search_url = base_url.rstrip("/") + "/rest/api/3/search/jql"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = HTTPBasicAuth(email, api_token)

    collected_issues: List[JiraIssue] = []
    next_page_token: Optional[str] = None

    while True:
        try:
            response: Response = requests.post(
                search_url,
                headers=headers,
                json=build_jira_request_body(project_key, next_page_token),
                auth=auth,
                timeout=http_timeout_seconds,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logging.error("Failed to reach Jira: %s", exc)
            break

        data: Dict[str, Any] = response.json() or {}
        batch: List[JiraIssue] = parse_jira_issues(data)
        collected_issues.extend(batch)

        is_last: bool = data.get("isLast", True)
        next_page_token = data.get("nextPageToken")

        if is_last or not next_page_token:
            break

    return collected_issues


def render_issue_as_single_line(issue: JiraIssue) -> str:
    # Produce a compact, human-friendly single-line representation of an issue.
    priority_suffix = f" [Priority: {issue.priority_name}]" if issue.priority_name else ""
    return (
        f"{issue.key}: {issue.summary} — {issue.status_name} "
        f"(Owner: {issue.assignee_display_name}, Due: {issue.due_date_human}){priority_suffix}"
    )


def build_tasks_text_block(issues: List[JiraIssue]) -> str:
    # Join all issues into a newline-separated block that can be fed to the LLM.
    if not issues:
        return "No issues were returned for the selected project."
    return "\n".join(render_issue_as_single_line(i) for i in issues)


def generate_weekly_report(
    tasks_block: str,
    model_name: str,
    optional_system_prompt: Optional[str],
) -> str:
    # Use OpenAI to summarize progress, surface risks, and propose next steps.
    # Returns a human-readable string (not markdown-sensitive).
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK missing. Install with: pip install openai")

    client = OpenAI()

    system_message = (optional_system_prompt or "").strip()
    user_message = (
        "Here are the tasks pulled from Jira (one per line). "
        "Summarize progress, call out risks (overdue/blocked/unassigned), and propose concrete next steps.\n\n"
        f"TASKS:\n{tasks_block}"
    )

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
        logging.error("OpenAI API error: %s", exc)
        return (
            "Overall Status\n- Unable to generate AI summary.\n\n"
            "Key Risks & Blockers\n- Review Jira connectivity or OpenAI key.\n\n"
            "Next Steps\n- Re-run the script after fixing configuration."
        )


def save_report(text: str, output_path: str):
    # Write the final report text to disk, logging a clear error if it fails.
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        logging.info("Report written to %s", output_path)
    except OSError as exc:
        logging.error("Failed to write report to %s: %s", output_path, exc)


def main():
    # Load environment variables from .env if present (no heavy logging config here)
    if os.path.exists(".env"):
        load_dotenv(".env")

    # Make logging visible if not configured elsewhere; honor LOG_LEVEL from env.
    ensure_basic_logging(os.getenv("LOG_LEVEL", "INFO"))

    ensure_config_variables_set([
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
        "OPENAI_API_KEY",
    ])

    jira_base_url = os.getenv("JIRA_BASE_URL", "").strip()
    jira_email = os.getenv("JIRA_EMAIL", "").strip()
    jira_api_token = os.getenv("JIRA_API_TOKEN", "").strip()
    jira_project_key = os.getenv("JIRA_PROJECT_KEY", "").strip()

    openai_model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    optional_system_prompt = os.getenv("AI_PROMPT")  # can be None
    output_file_path = os.getenv("REPORT_OUTPUT", "weekly_report.txt").strip()

    logging.info(
        "Fetching issues from Jira project '%s' via POST /rest/api/3/search/jql...",
        jira_project_key,
    )
    issues: List[JiraIssue] = fetch_all_jira_issues(
        base_url=jira_base_url,
        email=jira_email,
        api_token=jira_api_token,
        project_key=jira_project_key,
    )
    if not issues:
        logging.warning("No issues retrieved from Jira. Proceeding with an empty task list.")

    tasks_block: str = build_tasks_text_block(issues)

    logging.info("Generating weekly report with OpenAI model '%s'...", openai_model_name)
    ai_report_text: str = generate_weekly_report(
        tasks_block=tasks_block,
        model_name=openai_model_name,
        optional_system_prompt=optional_system_prompt,
    )

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    banner = f"Weekly Status Report for {jira_project_key} — generated {timestamp}\n"
    divider = "=" * len(banner)
    final_text_output = (
        f"\n{banner}{divider}\n\n{ai_report_text}\n\n---\nRaw Tasks\n{tasks_block}\n"
    )

    print(final_text_output)
    save_report(final_text_output, output_file_path)


if __name__ == "__main__":
    main()
