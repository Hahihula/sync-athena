"""CLI entrypoint: link an opened/reopened PR to its matching Athena task.

Reads ``$GITHUB_EVENT_PATH`` for a ``pull_request`` event, resolves the
Athena task it belongs to, and posts a note child under that task linking
to the PR. Athena has no discussion primitive, so the link is a plain
child node hanging off the task.

Resolution order, first hit wins:

    1. a ``<PREFIX>-NNNN`` key in the PR title,
    2. a ``#NNNN`` issue reference in the title or body — since the ticket
       number *is* the issue number, ``Closes #1044`` resolves to
       ``<PREFIX>-1044``,
    3. a ``<PREFIX>-NNNN`` key anywhere in the PR body.

Failure policy matches the rest of the action:

    * No task resolved → log "skipping", exit 0.
    * Athena API error → ``::warning::`` + exit 0 (never fail the workflow).
    * Note already posted for this PR → skip, so the three trigger types
      (opened / reopened / ready_for_review) can't stack up duplicates.
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
    find_task_by_key,
    find_tasks_folder,
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


def candidate_ticket_numbers(title: str, body: str, prefix: str) -> list[int]:
    """Ticket numbers this PR might belong to, best guess first."""
    ordered = [
        *extract_ticket_refs(title, prefix),
        *extract_issue_refs(title),
        *extract_issue_refs(body),
        *extract_ticket_refs(body, prefix),
    ]
    seen: list[int] = []
    for number in ordered:
        if number not in seen:
            seen.append(number)
    return seen


def tag_pr_on_task(
    client: AthenaClient, *, task_id: str, pr_number: int, db_path: str
) -> None:
    """Tag the parent task with ``github-pr-<n>`` so comment_to_task finds it.

    The API only offers PUT-replace for hashtags, so re-fetch and append.
    """
    target = pr_hashtag(pr_number)
    try:
        node = client.get_node(node_id=task_id, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::could not read task {task_id} to tag PR: {exc}")
        return
    if target in node.hashtags:
        return
    try:
        client.set_hashtags(
            node_id=task_id,
            hashtags=[*node.hashtags, target],
            db_path=db_path,
        )
    except AthenaError as exc:
        print(f"::warning::could not tag task with {target!r}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Path to GitHub event JSON (defaults to $GITHUB_EVENT_PATH)")
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
    pr = event.get("pull_request")
    if not pr:
        print("::warning::not a pull_request event — skipping", file=sys.stderr)
        return 0

    title = pr.get("title", "")
    body = pr.get("body") or ""
    number = int(pr.get("number", 0))
    html_url = pr.get("html_url", "")
    author_login = (pr.get("user") or {}).get("login", "")

    candidates = candidate_ticket_numbers(title, body, prefix)
    if not candidates:
        print(
            f"No Athena ticket reference found in PR #{number}: {title!r}. "
            f"Use a {prefix}-NNNN key in the title, or reference the issue "
            f"(e.g. 'Closes #1044')."
        )
        return 0

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            tasks_folder_id = find_tasks_folder(
                client, db_path=db_path, folder_name=folder_name
            )
            task = None
            for candidate in candidates:
                task = find_task_by_key(
                    client,
                    key=ticket_key(prefix, candidate),
                    prefix=prefix,
                    db_path=db_path,
                    tasks_folder_id=tasks_folder_id,
                )
                if task is not None:
                    break
            if task is None:
                tried = ", ".join(ticket_key(prefix, c) for c in candidates)
                print(f"::warning::no Athena task found for PR #{number} (tried {tried}) — skipping")
                return 0

            note_name = f"PR #{number}: {title}"
            if find_child_note(
                client, parent_id=task.id, name_prefix=f"PR #{number}:", db_path=db_path
            ):
                print(f"PR #{number} already linked to {task.name} — skipping")
                return 0

            client.create_child_note(
                parent_id=task.id,
                name=note_name,
                description=markdown_to_blocknote(
                    f"PR opened by @{author_login}: [{title}]({html_url})"
                ),
                db_path=db_path,
                author=author,
                node_type="solution",
            )
            tag_pr_on_task(
                client, task_id=task.id, pr_number=number, db_path=db_path
            )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    print(f"Linked PR #{number} to {task.name} (task {task.id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
