"""CLI entrypoint: append a GitHub comment to its matching Athena task.

Triggered by ``issue_comment`` events (fires for both issues and PRs).
Locates the existing task and creates a ``type=comment`` child node carrying
the comment body — the comment is treated as a child note, not as an
update to the task description.

Two lookup paths:

    * Issue comments: the parent task was created by ``issue_to_task`` and
      carries the ``github-issue-<n>`` hashtag, so we search by hashtag.
    * PR comments: the parent task carries ``github-pr-<n>`` (set by
      ``pr_to_comment``); same hashtag search. As a fallback we still try
      the PR-title regex so a ticket referenced only by ``<PREFIX>-NNNN:``
      in the PR title resolves correctly.

Mirrors the existing failure policy:

    * Event has no ``issue`` field → log + exit 0.
    * Task not found → log + exit 0 (comment was posted on a ticket we
      never created; nothing to do).
    * Athena API error → ``::warning::`` + exit 0.
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
    extract_pr_key,
    find_task_by_hashtag,
    pr_hashtag,
)

DEFAULT_AUTHOR = "github-actions@users.noreply.github.com"


def read_event(event_path: str | None) -> dict[str, Any]:
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        print(
            "::error::GITHUB_EVENT_PATH is not set and --event was not given",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(Path(path).read_text())


def resolve_task_id(
    client: AthenaClient,
    *,
    issue_number: int,
    is_pull_request: bool,
    issue_title: str,
    prefix: str,
    db_path: str,
) -> str | None:
    """Find the parent task for an issue_comment event.

    For issues and PRs alike, the preferred path is a hashtag lookup; if
    that misses we try the PR-title regex. Returns ``None`` when no task
    can be resolved.
    """
    if is_pull_request:
        by_hashtag = find_task_by_hashtag(
            client, db_path=db_path, hashtag=pr_hashtag(issue_number)
        )
        if by_hashtag is not None:
            return by_hashtag.id
    else:
        by_hashtag = find_task_by_hashtag(
            client, db_path=db_path, hashtag=f"github-issue-{issue_number}"
        )
        if by_hashtag is not None:
            return by_hashtag.id

    if is_pull_request:
        key_number = extract_pr_key(issue_title, prefix)
        if key_number is None:
            return None
        key = f"{prefix}-{key_number}"
        matches = client.search_tasks(
            hashtag=f"{prefix.strip().lower()}",
            db_path=db_path,
            extra_query=key,
        )
        candidates = [n for n in matches if n.name.startswith(f"{key}:")]
        if len(candidates) == 1:
            return candidates[0].id
        if len(candidates) > 1:
            print(
                f"::warning::multiple tasks match PR key {key!r}: "
                + ", ".join(n.name for n in candidates),
                file=sys.stderr,
            )
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

    issue_number = int(issue.get("number", 0))
    issue_title = issue.get("title", "")
    is_pull_request = bool(issue.get("pull_request"))
    comment_author = (comment.get("user") or {}).get("login", "")
    comment_body = comment.get("body", "") or ""
    comment_url = comment.get("html_url", "")

    if not comment_body.strip():
        print("::warning::empty comment body, skipping")
        return 0

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            task_id = resolve_task_id(
                client,
                issue_number=issue_number,
                is_pull_request=is_pull_request,
                issue_title=issue_title,
                prefix=prefix,
                db_path=db_path,
            )
            if task_id is None:
                print(
                    f"::warning::no Athena task found for "
                    f"{'PR' if is_pull_request else 'issue'} #{issue_number} — skipping"
                )
                return 0

            prefix_label = "PR" if is_pull_request else "issue"
            comment_name = f"Comment on {prefix_label} #{issue_number}"
            body_md = (
                f"Comment by @{comment_author} on "
                f"[{prefix_label} #{issue_number}]({comment_url})\n\n"
                f"{comment_body}"
            )
            client.create_comment_child(
                parent_id=task_id,
                name=comment_name,
                description=markdown_to_blocknote(body_md),
                db_path=db_path,
                author=author,
            )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    kind = "PR" if is_pull_request else "issue"
    print(f"Appended comment to {kind} #{issue_number} (task {task_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
