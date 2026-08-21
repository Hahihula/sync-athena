"""Attach editors / co-authors to a node, additively and verifiably.

Three things the server does that a naive PUT gets wrong:

    1. ``apply_to_descendants`` is not optional in the request body.
    2. A node's own author may not appear in either list — the whole
       request is rejected, not just that entry.
    3. The lists are replaced wholesale, so a plain PUT silently drops
       collaborators added by hand in the UI.

So we read the current lists, merge, drop the author, and read back to
confirm. Failures stay non-fatal (``::warning::`` + carry on) but say
exactly which address the server refused and why.
"""

from __future__ import annotations

from sync_athena.athena_client import AthenaClient, AthenaError, Collaborators


def parse_list(raw: str, *, email_domain: str = "") -> list[str]:
    """Split a comma-separated ``editors``/``co_authors`` value into emails.

    Bare usernames are expanded against ``email_domain`` when one is
    configured, matching athena-client — ``petr.gadorek`` and
    ``petr.gadorek@espressif.com`` should not behave differently.
    """
    out: list[str] = []
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if "@" not in entry and email_domain:
            entry = f"{entry}@{email_domain.lstrip('@')}"
        if entry not in out:
            out.append(entry)
    return out


def _merge(current: list[str], additions: list[str], *, exclude: list[str]) -> list[str]:
    merged = [e for e in current if e not in exclude]
    for entry in additions:
        if entry not in merged and entry not in exclude:
            merged.append(entry)
    return merged


def apply_collaborators(
    client: AthenaClient,
    *,
    node_id: str,
    editors: list[str],
    co_authors: list[str],
    db_path: str,
) -> Collaborators | None:
    """Add ``editors`` / ``co_authors`` to a node, keeping existing entries.

    Returns the resulting lists, or None when nothing could be applied.
    """
    if not editors and not co_authors:
        return None

    try:
        current = client.get_collaborators(node_id=node_id, db_path=db_path)
    except AthenaError as exc:
        print(f"::warning::could not read collaborators of {node_id}: {exc}")
        return None

    author = current.author
    dropped = [e for e in (*editors, *co_authors) if e == author]
    if dropped:
        print(
            f"::notice::skipping {', '.join(dropped)} — already the author of "
            f"this task; the server rejects the author as a collaborator"
        )

    # A user named in both roles ends up a co-author: it is the stronger role.
    want_co_authors = _merge(current.co_authors, co_authors, exclude=[author])
    want_editors = _merge(
        current.editors, editors, exclude=[author, *want_co_authors]
    )

    if want_co_authors == current.co_authors and want_editors == current.editors:
        return current

    if not current.manageable:
        print(
            f"::warning::the Athena token may not manage collaborators on "
            f"{node_id} (node author is {author or 'unknown'!r}) — only the "
            f"author and project admins can. Attempting anyway."
        )

    try:
        result = client.set_collaborators(
            node_id=node_id,
            editors=want_editors,
            co_authors=want_co_authors,
            db_path=db_path,
        )
    except AthenaError as exc:
        print(
            f"::warning::could not set collaborators on {node_id} "
            f"(editors={want_editors!r}, co_authors={want_co_authors!r}): {exc}"
        )
        return None

    missing = [c for c in co_authors if c != author and c not in result.co_authors]
    if missing:
        print(
            f"::warning::the server accepted the request but did not add "
            f"{', '.join(missing)} as co-authors — check they are registered "
            f"Athena users and members of this project"
        )
    return result
