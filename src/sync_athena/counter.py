"""Pick the next free <PREFIX>-XXXX number and create a task under it.

The "next free number" isn't a built-in Athena concept — nodes have UUIDs and
weblink short IDs, not sequential tickets. We compute the number by:

    1. Search for all tasks under the project carrying the ticket hashtag
       (``node_type=task`` + ``hashtag=<prefix-lowercased>`` filters server-side).
    2. Parse names with ``^<PREFIX>-(\\d+):``.
    3. Take the max, +1.

Two issues opened in quick succession could in principle pick the same number
before either has committed. The retry loop in :func:`create_ticket` handles
that: on failure, re-pick the next free number and retry. Bounded by
``max_retries`` to avoid pathological loops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sync_athena.athena_client import AthenaClient, AthenaError, Node


def make_ticket_name_regex(prefix: str) -> re.Pattern[str]:
    """Compile a regex matching ``<PREFIX>-<digits>`` at the start of a name.

    ``re.escape`` keeps prefixes like ``EIM-FOO`` (unusual but legal) from
    breaking the matcher.
    """
    return re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\s|:|$)")


def make_pr_title_regex(prefix: str) -> re.Pattern[str]:
    """Compile a regex matching ``<PREFIX>-<digits>:`` at the start of a title.

    Slightly stricter than the name regex: requires the colon so a PR titled
    ``EIM-1234 something`` doesn't accidentally match.
    """
    return re.compile(rf"^{re.escape(prefix)}-(\d+)\s*:")


def extract_ticket_number(name: str, prefix: str) -> int | None:
    """Return the numeric part of a ``<PREFIX>-NNNN: ...`` name, or None."""
    match = make_ticket_name_regex(prefix).match(name.strip())
    if not match:
        return None
    return int(match.group(1))


def extract_pr_key(title: str, prefix: str) -> int | None:
    """Return the ``<PREFIX>-NNNN`` number from a PR title, or None."""
    match = make_pr_title_regex(prefix).match(title.strip())
    if not match:
        return None
    return int(match.group(1))


def hashtag_for_prefix(prefix: str) -> str:
    """Derive the Athena hashtag used to index tickets for ``prefix``.

    Lower-cased and stripped; the server is case-sensitive on hashtags, so we
    always normalize to one canonical form.
    """
    return prefix.strip().lower()


def issue_hashtag(issue_number: int) -> str:
    """Hashtag used to link a ticket back to its GitHub issue number."""
    return f"github-issue-{issue_number}"


def pr_hashtag(pr_number: int) -> str:
    """Hashtag used to link a ticket back to its GitHub PR number."""
    return f"github-pr-{pr_number}"


def find_task_by_hashtag(
    client: AthenaClient, *, db_path: str, hashtag: str
) -> Node | None:
    """Return the single task tagged with ``hashtag``, or None.

    Used to look up an existing ticket for a known GitHub issue/PR number
    so ``issue_to_task`` can update in place instead of creating duplicates.
    Raises if the hashtag resolves to more than one task — that should never
    happen if the workflow only runs against one Athena project per prefix.
    """
    matches = client.search_tasks(hashtag=hashtag, db_path=db_path)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AthenaError(
            status_code=500,
            url=f"/api/nodes/advanced_search?hashtag={hashtag}",
            body=f"hashtag {hashtag!r} matches {len(matches)} tasks; expected 1",
        )
    return None


def next_ticket_number(
    client: AthenaClient, *, db_path: str, prefix: str
) -> int:
    """Return the next free ``<PREFIX>-NNNN`` number.

    Returns 1 when no existing tickets are found. The search is server-side
    filtered by ``hashtag=<prefix-lowercased> + node_type=task``, so it stays
    cheap even on a large project.
    """
    if not prefix:
        raise ValueError("prefix must be a non-empty string")
    matches = client.search_tasks(
        hashtag=hashtag_for_prefix(prefix), db_path=db_path
    )
    max_seen = 0
    for node in matches:
        number = extract_ticket_number(node.name, prefix)
        if number is not None and number > max_seen:
            max_seen = number
    return max_seen + 1


@dataclass
class CreatedTicket:
    """The result of :func:`create_ticket` — the new node and its key."""

    number: int
    key: str
    node: Node


def create_ticket(
    client: AthenaClient,
    *,
    title: str,
    body_markdown: str,
    db_path: str,
    tasks_folder_id: str,
    author: str,
    prefix: str,
    issue_number: int | None = None,
    initial_status: str = "TODO",
    body_to_blocknote,
    max_retries: int = 3,
) -> CreatedTicket:
    """Create the next ``<PREFIX>-NNNN: <title>`` task with retry on collision.

    ``body_to_blocknote`` is the ``markdown_to_blocknote`` callable — passed in
    rather than imported here so callers can swap it out in tests.

    Raises ``AthenaError`` if every retry fails.
    """
    if not prefix:
        raise ValueError("prefix must be a non-empty string")

    last_error: Exception | None = None
    for _attempt in range(max_retries):
        number = next_ticket_number(client, db_path=db_path, prefix=prefix)
        key = f"{prefix}-{number}"
        name = f"{key}: {title}"
        hashtags = [hashtag_for_prefix(prefix)]
        if issue_number is not None:
            hashtags.append(f"github-issue-{issue_number}")

        description = body_to_blocknote(body_markdown)
        try:
            node = client.create_task(
                parent_id=tasks_folder_id,
                name=name,
                description=description,
                db_path=db_path,
                author=author,
                status=initial_status,
                hashtags=hashtags,
            )
            return CreatedTicket(number=number, key=key, node=node)
        except AthenaError as exc:
            last_error = exc
            continue
    raise AthenaError(
        status_code=last_error.status_code if last_error else 0,
        url=last_error.url if last_error else "",
        body=f"failed to create ticket after {max_retries} attempts: {last_error}",
    )