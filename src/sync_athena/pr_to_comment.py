"""CLI entrypoint: link an opened/reopened PR to its matching Athena task.

Reads ``$GITHUB_EVENT_PATH`` for a ``pull_request`` event, extracts the
``<PREFIX>-NNNN`` key from the PR title (e.g. ``EIM-1234: title``), resolves
it to the corresponding Athena task, and posts a ``type=comment`` child node
under that task with a link to the PR.

Mirrors the previous Jira PR-comment workflow's failure policy:

    * No key found → log "skipping", exit 0.
    * Athena API error → ``::warning::`` + exit 0 (never fail the workflow).
    * Key resolves to multiple tasks → pick the one whose name starts with
      ``<PREFIX>-NNNN:`` exactly; if ambiguous, log a warning and skip.
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
    hashtag_for_prefix,
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


def find_task_by_key(
    client: AthenaClient, *, key: str, prefix: str, db_path: str
) -> str | None:
    """Resolve a ``<PREFIX>-NNNN`` key to a task node id.

    Two-step to avoid false positives:
        1. Search by hashtag ``<prefix-lowercased>`` + free text ``<key>``.
        2. Filter client-side for an exact name prefix ``<key>:``.

    Returns the task id, or None if not found / ambiguous.
    """
    matches = client.search_tasks(
        hashtag=hashtag_for_prefix(prefix),
        db_path=db_path,
        extra_query=key,
    )
    name_prefix = f"{key}:"
    candidates = [n for n in matches if n.name.startswith(name_prefix)]
    if len(candidates) == 1:
        return candidates[0].id
    if len(candidates) > 1:
        print(
            f"::warning::multiple tasks match {key!r}: "
            + ", ".join(c.name for c in candidates),
            file=sys.stderr,
        )
        return None
    return None


def _tag_pr_on_task(
    client: AthenaClient,
    *,
    task_id: str,
    pr_number: int | None,
    db_path: str,
) -> None:
    """Tag the parent task with ``github-pr-<n>`` so comment_to_task finds it.

    The local ``AthenaClient`` only exposes PUT-replace for hashtags, so we
    re-fetch the current list via ``get_node`` and append. Failure here is
    non-fatal — the comment_to_task flow has a regex-based fallback.
    """
    if pr_number is None:
        return
    target = pr_hashtag(int(pr_number))
    try:
        node = client.get_node(node_id=task_id, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::could not read task {task_id} to tag PR: {exc}")
        return
    existing = node.raw.get("hashtags") or []
    if target in existing:
        return
    try:
        client.set_hashtags(
            node_id=task_id,
            hashtags=[*existing, target],
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
    number = pr.get("number")
    html_url = pr.get("html_url", "")
    author_login = (pr.get("user") or {}).get("login", "")

    key_number = extract_pr_key(title, prefix)
    if key_number is None:
        print(
            f"No Athena key found in PR title: {title!r}. "
            f"Expected format: {prefix}-NNNN: description"
        )
        return 0
    key = f"{prefix}-{key_number}"

    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            task_id = find_task_by_key(
                client, key=key, prefix=prefix, db_path=db_path
            )
            if task_id is None:
                print(
                    f"::warning::Athena task {key} not found for PR #{number} — skipping"
                )
                return 0

            body_md = (
                f"PR opened by @{author_login}: [{title}]({html_url})"
            )
            comment_name = f"PR #{number}: {title}"
            client.create_comment_child(
                parent_id=task_id,
                name=comment_name,
                description=markdown_to_blocknote(body_md),
                db_path=db_path,
                author=author,
            )
            _tag_pr_on_task(
                client,
                task_id=task_id,
                pr_number=number,
                db_path=db_path,
            )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    print(f"Linked PR #{number} to {key} (task {task_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())