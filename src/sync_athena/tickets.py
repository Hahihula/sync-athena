"""Ticket keys, GitHub reference parsing, and duplicate detection.

A ticket key is ``<PREFIX>-<github issue number>``: issue
``github.com/espressif/idf-im-ui/issues/1044`` is always ``EIM-1044``.
Deriving the number from GitHub rather than counting existing Athena tasks
means the key is stable, reproducible from the issue URL alone, and immune
to the race where two issues opened together both pick the same "next"
number.

Duplicate detection reads the tasks folder directly instead of trusting
``advanced_search``: a task is a duplicate if a sibling is already named
``<PREFIX>-<n>`` or ``<PREFIX>-<n>: ...``. The hashtag search is kept only
as a fallback for tasks that were moved out of the folder by hand.
"""

from __future__ import annotations

import re

from sync_athena.athena_client import AthenaClient, AthenaError, Node

ISSUE_URL_RE = re.compile(r"/(?:issues|pull)/(\d+)")
ISSUE_REF_RE = re.compile(r"#(\d+)")


def ticket_key(prefix: str, number: int) -> str:
    """Build the ``<PREFIX>-<number>`` key."""
    return f"{prefix}-{number}"


def make_ticket_name_regex(prefix: str) -> re.Pattern[str]:
    """Compile a regex matching ``<PREFIX>-<digits>`` at the start of a name.

    ``re.escape`` keeps prefixes like ``EIM-FOO`` (unusual but legal) from
    breaking the matcher.
    """
    return re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\s|:|$)")


def extract_ticket_number(name: str, prefix: str) -> int | None:
    """Return the numeric part of a ``<PREFIX>-NNNN: ...`` name, or None."""
    match = make_ticket_name_regex(prefix).match(name.strip())
    if not match:
        return None
    return int(match.group(1))


def issue_number_from_url(url: str) -> int | None:
    """Pull the number out of a GitHub issue or PR URL.

    ``https://github.com/owner/repo/issues/1044`` -> ``1044``. Pull URLs
    (``/pull/1044``) parse too, so the same helper serves both events.
    """
    match = ISSUE_URL_RE.search(url or "")
    if not match:
        return None
    return int(match.group(1))


def extract_ticket_refs(text: str, prefix: str) -> list[int]:
    """Return every ``<PREFIX>-NNNN`` number mentioned in ``text``, in order."""
    pattern = re.compile(rf"\b{re.escape(prefix)}-(\d+)\b")
    return [int(m) for m in pattern.findall(text or "")]


def extract_issue_refs(text: str) -> list[int]:
    """Return every ``#NNNN`` issue reference in ``text``, in order.

    Used to resolve a PR to its ticket when the title carries no key: since
    the ticket number *is* the issue number, ``Closes #1044`` is enough.
    """
    return [int(m) for m in ISSUE_REF_RE.findall(text or "")]


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


def ticket_hashtags(prefix: str, issue_number: int) -> list[str]:
    """The full hashtag set every ticket carries."""
    return [hashtag_for_prefix(prefix), issue_hashtag(issue_number)]


def _pick_one(matches: list[Node], what: str) -> Node | None:
    """Return the first match, warning when the lookup was ambiguous.

    Never raises: an ambiguous lookup must still block creation, because
    creating yet another task is strictly worse than updating the wrong one
    of two existing duplicates.
    """
    if not matches:
        return None
    if len(matches) > 1:
        print(
            f"::warning::{what} resolves to {len(matches)} tasks "
            f"({', '.join(n.name for n in matches)}); using the first and "
            f"not creating a new one"
        )
    return matches[0]


def find_tasks_folder(
    client: AthenaClient, *, db_path: str, folder_name: str
) -> str | None:
    """Return the id of the tasks folder, or None if it doesn't exist yet."""
    root = client.get_project_root(db_path=db_path)
    folder = client.find_child_named(
        parent_id=root.id, name=folder_name, db_path=db_path
    )
    return folder.id if folder is not None else None


def ensure_tasks_folder(
    client: AthenaClient, *, db_path: str, folder_name: str, author: str
) -> str:
    """Return the id of the tasks folder, creating it on first use."""
    folder_id = find_tasks_folder(
        client, db_path=db_path, folder_name=folder_name
    )
    if folder_id is not None:
        return folder_id
    root = client.get_project_root(db_path=db_path)
    return client.create_child_folder(
        parent_id=root.id, name=folder_name, db_path=db_path, author=author
    ).id


def find_task_by_hashtag(
    client: AthenaClient, *, db_path: str, hashtag: str
) -> Node | None:
    """Return the task tagged with ``hashtag``, or None."""
    try:
        matches = client.search_tasks(hashtag=hashtag, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::hashtag search for {hashtag!r} failed: {exc}")
        return None
    return _pick_one(matches, f"hashtag {hashtag!r}")


def find_task_for_issue(
    client: AthenaClient,
    *,
    db_path: str,
    tasks_folder_id: str,
    prefix: str,
    issue_number: int,
) -> Node | None:
    """Find the task already tracking ``issue_number``, or None.

    Folder scan first (authoritative — it is the tree itself), hashtag
    search second (catches tasks somebody dragged elsewhere).
    """
    found = find_task_by_key(
        client,
        key=ticket_key(prefix, issue_number),
        prefix=prefix,
        db_path=db_path,
        tasks_folder_id=tasks_folder_id,
    )
    if found is not None:
        return found
    return find_task_by_hashtag(
        client, db_path=db_path, hashtag=issue_hashtag(issue_number)
    )


def find_task_by_key(
    client: AthenaClient,
    *,
    key: str,
    prefix: str,
    db_path: str,
    tasks_folder_id: str | None = None,
) -> Node | None:
    """Resolve a ``<PREFIX>-NNNN`` key to a task.

    Scans the tasks folder when its id is known, then falls back to the
    project-wide search for tickets that live somewhere else.
    """
    if tasks_folder_id:
        children = client.list_children(
            parent_id=tasks_folder_id, db_path=db_path
        )
        named = [
            n for n in children if n.name == key or n.name.startswith(f"{key}:")
        ]
        found = _pick_one(named, f"ticket key {key!r}")
        if found is not None:
            return found
    try:
        matches = client.search_tasks(
            hashtag=hashtag_for_prefix(prefix), db_path=db_path, extra_query=key
        )
    except AthenaError as exc:
        print(f"::warning::search for {key!r} failed: {exc}")
        return None
    named = [n for n in matches if n.name == key or n.name.startswith(f"{key}:")]
    return _pick_one(named, f"ticket key {key!r}")


def find_child_note(
    client: AthenaClient, *, parent_id: str, name_prefix: str, db_path: str
) -> Node | None:
    """Find a note child whose name starts with ``name_prefix``.

    Prefix rather than exact match because note names carry the GitHub
    title after the identifying part (``PR #12: <title>``), and an edited
    title must not make an already-posted note look absent.
    """
    try:
        children = client.list_children(parent_id=parent_id, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::could not list children of {parent_id}: {exc}")
        return None
    for node in children:
        if node.name.startswith(name_prefix):
            return node
    return None


def create_ticket(
    client: AthenaClient,
    *,
    title: str,
    description: str,
    db_path: str,
    tasks_folder_id: str,
    author: str,
    prefix: str,
    issue_number: int,
    initial_status: str = "TODO",
) -> Node:
    """Create the ``<PREFIX>-<issue_number>: <title>`` task."""
    key = ticket_key(prefix, issue_number)
    return client.create_task(
        parent_id=tasks_folder_id,
        name=f"{key}: {title}",
        description=description,
        db_path=db_path,
        author=author,
        status=initial_status,
        hashtags=ticket_hashtags(prefix, issue_number),
    )
