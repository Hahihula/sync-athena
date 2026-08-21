"""CLI entrypoint: create or update one Athena task from one GitHub issue.

Reads the GitHub event payload from ``$GITHUB_EVENT_PATH`` (or from ``--event``
if set) and either:

    * when ``--issue-number`` is given, builds a synthetic issue from
      workflow_dispatch inputs (backfill mode), or
    * when no flag is set, treats the event as an ``issues`` event and reads
      the issue from the JSON payload.

The ticket key is ``<PREFIX>-<issue number>`` — issue ``/issues/1044``
becomes ``EIM-1044``. Before creating anything the tasks folder is scanned
for that key, so re-running against the same issue updates the existing task
in place instead of filing a second one.

Writes to ``$GITHUB_OUTPUT`` in the format the workflow expects:

    ticket_key=<PREFIX>-1044
    ticket_number=1044
    task_id=<athena node uuid>
    task_url=<athena weblink url, may be empty if shortlink not yet created>

On any non-fatal Athena error, prints ``::warning::`` and exits 0 — matches
the existing Jira workflow's "never fail the build" policy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sync_athena import AthenaClient, AthenaError, Node, markdown_to_blocknote
from sync_athena.collaborators import apply_collaborators, parse_list
from sync_athena.tickets import (
    create_ticket,
    ensure_tasks_folder,
    find_task_for_issue,
    issue_number_from_url,
    ticket_hashtags,
    ticket_key,
)

DEFAULT_TASKS_FOLDER = "📝 Tasks"
DEFAULT_AUTHOR = "GithubBot"


def read_event(event_path: str | None) -> dict[str, Any]:
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        print(
            "::error::GITHUB_EVENT_PATH is not set and --event was not given",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(Path(path).read_text())


def extract_issue_from_event(event: dict[str, Any]) -> tuple[int, str, str]:
    """Pull (number, title, body) from an issues-event payload.

    The number comes from ``html_url`` when present — the URL is the
    canonical source for the ticket number, and it is what a human reads off
    the browser address bar when cross-checking a key.
    """
    issue = event.get("issue")
    if not issue:
        print(
            "::warning::event has no 'issue' field — not an issues event?",
            file=sys.stderr,
        )
        sys.exit(0)
    number = issue_number_from_url(issue.get("html_url", ""))
    if number is None:
        number = int(issue["number"])
    return number, issue["title"], issue.get("body") or ""


def emit_output(values: dict[str, str]) -> None:
    """Append to ``$GITHUB_OUTPUT`` if set, else print to stdout for tests."""
    out_path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in values.items()]
    if out_path:
        with open(out_path, "a") as f:
            for line in lines:
                f.write(line + "\n")
    else:
        for line in lines:
            print(line)


def _gh(args: list[str], *, check: bool = True) -> None:
    """Run a ``gh`` command with the workflow's token, non-fatally."""
    env = os.environ.copy()
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        env["GH_TOKEN"] = token
    try:
        subprocess.run(["gh", *args], check=check, env=env, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"::warning::gh {' '.join(args[:2])} failed: {exc}")


def post_issue_comment(repo: str, issue_number: int, body: str) -> None:
    _gh(["issue", "comment", str(issue_number), "--repo", repo, "--body", body])


def add_issue_label(repo: str, issue_number: int, label: str, color: str) -> None:
    """Create the label (idempotent) and add it to the issue."""
    _gh(
        [
            "label", "create", label,
            "--repo", repo,
            "--color", color,
            "--description", "Athena ticket reference",
        ],
        check=False,
    )
    _gh(["issue", "edit", str(issue_number), "--repo", repo, "--add-label", label])


def ensure_hashtags(
    client: AthenaClient, *, node: Node, prefix: str, issue_number: int, db_path: str
) -> None:
    """Backfill the ticket hashtags on a task that is missing them.

    An earlier run whose ``set_hashtags`` call failed leaves a task the
    hashtag search cannot see; repairing it here keeps the search fallback
    honest without a separate migration.
    """
    wanted = ticket_hashtags(prefix, issue_number)
    missing = [h for h in wanted if h not in node.hashtags]
    if not missing:
        return
    try:
        client.set_hashtags(
            node_id=node.id, hashtags=[*node.hashtags, *missing], db_path=db_path
        )
    except AthenaError as exc:
        print(f"::warning::could not repair hashtags on {node.name!r}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Path to GitHub event JSON (defaults to $GITHUB_EVENT_PATH)")
    parser.add_argument("--issue-number", type=int, help="Override issue number for backfill mode")
    parser.add_argument("--issue-title", help="Override title for backfill mode")
    parser.add_argument("--issue-body", default="", help="Override body for backfill mode")
    parser.add_argument("--repo", required=True, help="owner/repo slug")
    args = parser.parse_args()

    project_uuid = os.environ.get("ATHENA_PROJECT_UUID", "")
    db_path = os.environ.get("ATHENA_DB_PATH", "")
    base_url = os.environ.get("ATHENA_BASE_URL", "")
    token = os.environ.get("ATHENA_TOKEN", "")
    folder_name = os.environ.get("ATHENA_TASKS_FOLDER", DEFAULT_TASKS_FOLDER)
    author = os.environ.get("ATHENA_AUTHOR", DEFAULT_AUTHOR)
    prefix = os.environ.get("ATHENA_TICKET_PREFIX", "")
    email_domain = os.environ.get("ATHENA_EMAIL_DOMAIN", "")
    editors = parse_list(os.environ.get("ATHENA_EDITORS", ""), email_domain=email_domain)
    co_authors = parse_list(os.environ.get("ATHENA_CO_AUTHORS", ""), email_domain=email_domain)

    if not all([project_uuid, db_path, base_url, token]):
        print(
            "::error::ATHENA_PROJECT_UUID, ATHENA_DB_PATH, ATHENA_BASE_URL, "
            "ATHENA_TOKEN must all be set",
            file=sys.stderr,
        )
        return 1
    if not prefix:
        print(
            "::error::ATHENA_TICKET_PREFIX must be set "
            "(e.g. 'EIM', 'ESP') — refusing to default to a specific project's prefix",
            file=sys.stderr,
        )
        return 1

    if args.issue_number is not None:
        issue_number = args.issue_number
        title = args.issue_title or f"Backfilled issue #{issue_number}"
        body = args.issue_body
    else:
        event = read_event(args.event)
        issue_number, title, body = extract_issue_from_event(event)

    key = ticket_key(prefix, issue_number)
    description = markdown_to_blocknote(body)

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            tasks_folder_id = ensure_tasks_folder(
                client,
                db_path=db_path,
                folder_name=folder_name,
                author=author,
            )
            existing = find_task_for_issue(
                client,
                db_path=db_path,
                tasks_folder_id=tasks_folder_id,
                prefix=prefix,
                issue_number=issue_number,
            )

            if existing is not None:
                task = client.update_node(
                    node_id=existing.id,
                    name=f"{key}: {title}",
                    description=description,
                    db_path=db_path,
                )
                ensure_hashtags(
                    client,
                    node=existing,
                    prefix=prefix,
                    issue_number=issue_number,
                    db_path=db_path,
                )
            else:
                task = create_ticket(
                    client,
                    title=title,
                    description=description,
                    db_path=db_path,
                    tasks_folder_id=tasks_folder_id,
                    author=author,
                    prefix=prefix,
                    issue_number=issue_number,
                )

            apply_collaborators(
                client,
                node_id=task.id,
                editors=editors,
                co_authors=co_authors,
                db_path=db_path,
            )
            task_url = client.create_shortlink(node_id=task.id, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    emit_output(
        {
            "ticket_key": key,
            "ticket_number": str(issue_number),
            "task_id": task.id,
            "task_url": task_url or "",
        }
    )

    if existing is not None:
        print(f"Updated {key} ({task.id}) for issue #{issue_number}")
        return 0

    url_suffix = f" — {task_url}" if task_url else ""
    post_issue_comment(
        args.repo,
        issue_number,
        f"Created Athena task **{key}**: {title}{url_suffix}\n\n"
        f"Reference this key in PR titles (e.g. `{key}: <description>`) "
        f"to link future PRs to this task.",
    )
    add_issue_label(args.repo, issue_number, key, "1d76db")
    print(f"Created {key} ({task.id}) for issue #{issue_number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
