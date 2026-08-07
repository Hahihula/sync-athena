"""CLI entrypoint: create or update one Athena task from one GitHub issue.

Reads the GitHub event payload from ``$GITHUB_EVENT_PATH`` (or from ``--event``
if set) and either:

    * when ``--issue-number`` is given, builds a synthetic issue from
      workflow_dispatch inputs (backfill mode), or
    * when no flag is set, treats the event as an ``issues`` event and reads
      the issue from the JSON payload.

The flow is idempotent: if a task already exists for the issue number
(identified by the ``github-issue-<n>`` hashtag), the existing task is
updated in place (name + description) instead of creating a duplicate.
This makes it safe to trigger on ``issues: [opened, edited, reopened]``
without producing a fresh ticket for each event.

Writes to ``$GITHUB_OUTPUT`` in the format the workflow expects:

    ticket_key=<PREFIX>-1234
    ticket_number=1234
    task_id=<athena node uuid>
    task_url=<athena weblink url, may be empty if shortlink not yet created>

On any non-fatal Athena error, prints ``::warning::`` and exits 0 — matches
the existing Jira workflow's "never fail the build" policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sync_athena import AthenaClient, AthenaError, markdown_to_blocknote
from sync_athena.counter import (
    create_ticket,
    extract_ticket_number,
    find_task_by_hashtag,
    issue_hashtag,
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
    """Pull (number, title, body) from an issues-event payload."""
    issue = event.get("issue")
    if not issue:
        print(
            "::warning::event has no 'issue' field — not an issues event?",
            file=sys.stderr,
        )
        sys.exit(0)
    return int(issue["number"]), issue["title"], issue.get("body") or ""


def ensure_tasks_folder(
    client: AthenaClient,
    *,
    db_path: str,
    folder_name: str,
    author: str,
) -> str:
    """Return the id of the tasks folder, creating it on first use."""
    root = client.get_project_root(db_path=db_path)
    folder = client.find_child_named(
        parent_id=root.id,
        name=folder_name,
        db_path=db_path,
    )
    if folder is not None:
        return folder.id
    folder = client.create_child_folder(
        parent_id=root.id,
        name=folder_name,
        db_path=db_path,
        author=author,
    )
    return folder.id


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


def parse_collaborators(raw: str) -> list[str]:
    """Split a comma-separated ``editors``/``co_authors`` env value into emails.

    Empty string and whitespace-only entries are dropped. Server rejects
    the node's own author and unknown users — both surfaced as
    ``AthenaError`` from ``set_collaborators`` so they fall into the
    existing ``::warning::`` + exit 0 path.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def post_issue_comment(repo: str, issue_number: int, body: str) -> None:
    """Post a comment on the GitHub issue using ``gh`` (already on the runner).

    Uses the GITHUB_TOKEN exposed to the workflow. Falls back silently if
    ``gh`` is unavailable — callers shouldn't crash on a comment failure.
    """
    import subprocess

    token = os.environ.get("GITHUB_TOKEN", "")
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    try:
        subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo,
                "--body",
                body,
            ],
            check=True,
            env=env,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"::warning::could not post GitHub issue comment: {exc}")


def add_issue_label(repo: str, issue_number: int, label: str, color: str) -> None:
    """Create the label (idempotent) and add it to the issue via ``gh``."""
    import subprocess

    token = os.environ.get("GITHUB_TOKEN", "")
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    try:
        subprocess.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                "Athena ticket reference",
            ],
            check=False,
            env=env,
            capture_output=True,
        )
        subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--repo",
                repo,
                "--add-label",
                label,
            ],
            check=True,
            env=env,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"::warning::could not add label {label!r}: {exc}")


def apply_collaborators(
    client: AthenaClient,
    *,
    node_id: str,
    editors: list[str],
    co_authors: list[str],
    db_path: str,
) -> None:
    """Set collaborator lists if any were provided.

    Failures are surfaced as ``::warning::`` so a misconfigured editor
    list doesn't fail the workflow — matches the rest of the action's
    failure model.
    """
    if not editors and not co_authors:
        return
    try:
        client.set_collaborators(
            node_id=node_id,
            editors=editors,
            co_authors=co_authors,
            db_path=db_path,
        )
    except AthenaError as exc:
        print(
            f"::warning::could not set collaborators "
            f"(editors={editors!r}, co_authors={co_authors!r}): {exc}"
        )


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
    editors = parse_collaborators(os.environ.get("ATHENA_EDITORS", ""))
    co_authors = parse_collaborators(os.environ.get("ATHENA_CO_AUTHORS", ""))

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

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            existing = find_task_by_hashtag(
                client, db_path=db_path, hashtag=issue_hashtag(issue_number)
            )
            if existing is not None:
                number = extract_ticket_number(existing.name, prefix)
                if number is None:
                    print(
                        f"::warning::existing task {existing.id} for issue "
                        f"#{issue_number} has no parseable ticket number in name "
                        f"{existing.name!r}; skipping in-place update"
                    )
                    return 0
                key = f"{prefix}-{number}"
                desired_name = f"{key}: {title}"
                client.update_node(
                    node_id=existing.id,
                    name=desired_name,
                    description=markdown_to_blocknote(body),
                    db_path=db_path,
                )
                task_url = client.create_shortlink(
                    node_id=existing.id, db_path=db_path
                )
                emit_output(
                    {
                        "ticket_key": key,
                        "ticket_number": str(number),
                        "task_id": existing.id,
                        "task_url": task_url or "",
                    }
                )
                apply_collaborators(
                    client,
                    node_id=existing.id,
                    editors=editors,
                    co_authors=co_authors,
                    db_path=db_path,
                )
                print(f"Updated {key} ({existing.id}) for issue #{issue_number}")
                return 0

            tasks_folder_id = ensure_tasks_folder(
                client,
                db_path=db_path,
                folder_name=folder_name,
                author=author,
            )
            result = create_ticket(
                client,
                title=title,
                body_markdown=body,
                db_path=db_path,
                tasks_folder_id=tasks_folder_id,
                author=author,
                prefix=prefix,
                issue_number=issue_number,
                body_to_blocknote=markdown_to_blocknote,
            )
            task_url = client.create_shortlink(
                node_id=result.node.id, db_path=db_path
            )
            emit_output(
                {
                    "ticket_key": result.key,
                    "ticket_number": str(result.number),
                    "task_id": result.node.id,
                    "task_url": task_url or "",
                }
            )
            apply_collaborators(
                client,
                node_id=result.node.id,
                editors=editors,
                co_authors=co_authors,
                db_path=db_path,
            )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    url_suffix = f" — {task_url}" if task_url else ""
    comment = (
        f"Created Athena task **{result.key}**: {title}{url_suffix}\n\n"
        f"Reference this key in PR titles (e.g. `{result.key}: <description>`) "
        f"to link future PRs to this task."
    )
    post_issue_comment(args.repo, issue_number, comment)
    add_issue_label(args.repo, issue_number, result.key, "1d76db")

    print(f"Created {result.key} ({result.node.id}) for issue #{issue_number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
