"""CLI entrypoint: mirror a GitHub comment as a child node of its task.

Triggered by ``issue_comment`` events (which fire for both issues and PRs).
Athena has no comment or discussion primitive, so each GitHub comment
becomes a plain child node hanging off the task — the task description
stays as the issue body while the conversation accumulates underneath.

Each note is named ``Comment #<github comment id>``, which makes the flow
idempotent: an edited comment updates the node it already created rather
than appending a second copy.

Two lookup paths:

    * Issue comments: the parent task was created by ``issue_to_task`` and
      is named ``<PREFIX>-<issue number>``, so the issue number resolves it
      directly; the ``github-issue-<n>`` hashtag is the fallback.
    * PR comments: the parent task carries ``github-pr-<n>`` (set by
      ``pr_to_comment``); failing that, ``<PREFIX>-NNNN`` or ``#NNNN``
      references in the PR title.

Anything unresolved is logged and skipped with exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sync_athena import AthenaClient, AthenaError, markdown_to_blocknote
from sync_athena.tickets import (
    extract_issue_refs,
    extract_ticket_refs,
    find_child_note,
    find_task_by_hashtag,
    find_task_by_key,
    find_tasks_folder,
    issue_hashtag,
    issue_number_from_url,
    pr_hashtag,
    ticket_key,
)

DEFAULT_AUTHOR = "github-actions@users.noreply.github.com"
DEFAULT_TASKS_FOLDER = "📝 Tasks"


def read_event(event_path: str | None) -> dict[str, Any]:
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        print(
            "::error::GITHUB_EVENT_PATH is not set and --event was not given",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(Path(path).read_text())


def resolve_task(
    client: AthenaClient,
    *,
    issue_number: int,
    is_pull_request: bool,
    issue_title: str,
    prefix: str,
    db_path: str,
    tasks_folder_id: str | None,
):
    """Find the task an ``issue_comment`` event belongs to, or None."""
    def by_key(number: int):
        return find_task_by_key(
            client,
            key=ticket_key(prefix, number),
            prefix=prefix,
            db_path=db_path,
            tasks_folder_id=tasks_folder_id,
        )

    if not is_pull_request:
        return by_key(issue_number) or find_task_by_hashtag(
            client, db_path=db_path, hashtag=issue_hashtag(issue_number)
        )

    by_hashtag = find_task_by_hashtag(
        client, db_path=db_path, hashtag=pr_hashtag(issue_number)
    )
    if by_hashtag is not None:
        return by_hashtag
    for candidate in (
        *extract_ticket_refs(issue_title, prefix),
        *extract_issue_refs(issue_title),
    ):
        task = by_key(candidate)
        if task is not None:
            return task
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        help="Path to GitHub event JSON (defaults to $GITHUB_EVENT_PATH)",
    )
    args = parser.parse_args()

    project_uuid = os.environ.get("ATHENA_PROJECT_UUID", "")
    db_path = os.environ.get("ATHENA_DB_PATH", "")
    base_url = os.environ.get("ATHENA_BASE_URL", "")
    token = os.environ.get("ATHENA_TOKEN", "")
    author = os.environ.get("ATHENA_AUTHOR", DEFAULT_AUTHOR)
    prefix = os.environ.get("ATHENA_TICKET_PREFIX", "")
    folder_name = os.environ.get("ATHENA_TASKS_FOLDER", DEFAULT_TASKS_FOLDER)

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

    event = read_event(args.event)
    issue = event.get("issue")
    comment = event.get("comment")
    if not issue or not comment:
        print(
            "::warning::event has no 'issue' or 'comment' field — not an issue_comment event?",
            file=sys.stderr,
        )
        return 0

    issue_number = issue_number_from_url(issue.get("html_url", ""))
    if issue_number is None:
        issue_number = int(issue.get("number", 0))
    issue_title = issue.get("title", "")
    is_pull_request = bool(issue.get("pull_request"))
    comment_id = comment.get("id")
    comment_author = (comment.get("user") or {}).get("login", "")
    comment_body = comment.get("body", "") or ""
    comment_url = comment.get("html_url", "")

    if not comment_body.strip():
        print("::warning::empty comment body, skipping")
        return 0

    kind = "PR" if is_pull_request else "issue"
    note_name = f"Comment #{comment_id} on {kind} #{issue_number}"
    note_body = markdown_to_blocknote(
        f"Comment by @{comment_author} on [{kind} #{issue_number}]({comment_url})\n\n"
        f"{comment_body}"
    )

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            task = resolve_task(
                client,
                issue_number=issue_number,
                is_pull_request=is_pull_request,
                issue_title=issue_title,
                prefix=prefix,
                db_path=db_path,
                tasks_folder_id=find_tasks_folder(
                    client, db_path=db_path, folder_name=folder_name
                ),
            )
            if task is None:
                print(
                    f"::warning::no Athena task found for {kind} #{issue_number} — skipping"
                )
                return 0

            existing = find_child_note(
                client,
                parent_id=task.id,
                name_prefix=f"Comment #{comment_id}",
                db_path=db_path,
            )
            if existing is not None:
                client.update_node(
                    node_id=existing.id,
                    name=note_name,
                    description=note_body,
                    db_path=db_path,
                )
                print(f"Updated comment #{comment_id} on {task.name} ({task.id})")
                return 0

            client.create_child_note(
                parent_id=task.id,
                name=note_name,
                description=note_body,
                db_path=db_path,
                author=author,
            )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    print(f"Appended comment #{comment_id} to {task.name} ({task.id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
