"""CLI entrypoint: create or update one Athena task from one GitHub issue.

Reads the GitHub event payload from ``$GITHUB_EVENT_PATH`` (or from ``--event``
if set) and either:

    * when ``--issue-number`` is given, syncs those issues (backfill mode).
      Accepts a comma-separated list, and fetches each issue's title and body
      from GitHub via ``gh`` so a backfilled ticket is indistinguishable from
      one filed by the ``issues`` trigger; or
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

Backfilling several issues at once emits the outputs of the last one — the
outputs describe "the ticket this run is about", which is only meaningful
for a single issue.

On any non-fatal Athena error, prints ``::warning::`` and exits 0 — matches
the existing Jira workflow's "never fail the build" policy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sync_athena import AthenaClient, AthenaError, Node, markdown_to_blocknote
from sync_athena.collaborators import apply_collaborators, parse_list
from sync_athena.tickets import (
    create_ticket,
    ensure_tasks_folder,
    find_task_for_issue,
    issue_number_from_url,
    status_for_issue,
    ticket_hashtags,
    ticket_key,
)

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


@dataclass
class GithubIssue:
    """The parts of a GitHub issue that map onto an Athena task."""

    number: int
    title: str
    body: str = ""
    #: Athena status to apply, or None to leave the task's status alone.
    status: str | None = None


def extract_issue_from_event(event: dict[str, Any]) -> GithubIssue:
    """Build a :class:`GithubIssue` from an issues-event payload.

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
    return GithubIssue(
        number=number,
        title=issue["title"],
        body=issue.get("body") or "",
        status=status_for_issue(
            action=event.get("action", ""),
            state=issue.get("state", ""),
            state_reason=issue.get("state_reason") or "",
        ),
    )


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


def _gh(args: list[str], *, check: bool = True) -> str | None:
    """Run a ``gh`` command with the workflow's token, non-fatally.

    Returns stdout, or None when ``gh`` is missing or the call failed.
    """
    env = os.environ.copy()
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    if token:
        env["GH_TOKEN"] = token
    try:
        done = subprocess.run(
            ["gh", *args], check=check, env=env, capture_output=True, text=True
        )
    except FileNotFoundError:
        print(f"::warning::gh is not installed; skipping `gh {' '.join(args[:2])}`")
        return None
    except subprocess.CalledProcessError as exc:
        # gh's own stderr says far more than the exit status does (status 4
        # is "not authenticated", i.e. no token reached this step).
        detail = (exc.stderr or "").strip() or f"exit status {exc.returncode}"
        hint = ""
        if exc.returncode == 4 and not token:
            hint = (
                " — no GITHUB_TOKEN reached this step; pass the "
                "`github_token` input to the action"
            )
        print(f"::warning::gh {' '.join(args[:2])} failed: {detail}{hint}")
        return None
    return done.stdout


def parse_issue_numbers(raw: str) -> list[int]:
    """Parse the ``issue_number`` input, which may be a comma-separated list.

    Non-numeric entries are reported and dropped rather than crashing the
    run — a typo in a manual backfill shouldn't lose the other issues.
    """
    numbers: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        entry = part.strip().lstrip("#")
        if not entry:
            continue
        if not entry.isdigit():
            print(f"::warning::ignoring non-numeric issue reference {part.strip()!r}")
            continue
        number = int(entry)
        if number not in numbers:
            numbers.append(number)
    return numbers


def fetch_issue(repo: str, number: int) -> GithubIssue:
    """Read an issue from GitHub for backfill mode.

    Includes the open/closed state, so backfilling an old closed issue
    files its ticket as DONE rather than TODO. Falls back to a placeholder
    title when ``gh`` is unavailable, so a backfill still produces a ticket
    that can be corrected later.
    """
    placeholder = GithubIssue(number=number, title=f"Backfilled issue #{number}")
    out = _gh(
        [
            "issue", "view", str(number),
            "--repo", repo,
            "--json", "title,body,state,stateReason",
        ],
        check=True,
    )
    if not out:
        return placeholder
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return placeholder
    return GithubIssue(
        number=number,
        title=data.get("title") or placeholder.title,
        body=data.get("body") or "",
        status=status_for_issue(
            state=data.get("state") or "",
            # gh reports NOT_PLANNED / COMPLETED; the webhook uses snake_case.
            state_reason=(data.get("stateReason") or "").lower(),
        ),
    )


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


def sync_issue(
    client: AthenaClient,
    *,
    repo: str,
    issue: GithubIssue,
    prefix: str,
    db_path: str,
    tasks_folder_id: str,
    author: str,
    editors: list[str],
    co_authors: list[str],
) -> dict[str, str]:
    """Create or update the Athena task for one GitHub issue.

    Returns the ``$GITHUB_OUTPUT`` values for this issue.
    """
    issue_number = issue.number
    key = ticket_key(prefix, issue_number)
    description = markdown_to_blocknote(issue.body)

    existing = find_task_for_issue(
        client,
        db_path=db_path,
        tasks_folder_id=tasks_folder_id,
        prefix=prefix,
        issue_number=issue_number,
    )

    if existing is not None:
        try:
            task = client.update_node(
                node_id=existing.id,
                name=f"{key}: {issue.title}",
                description=description,
                status=issue.status,
                db_path=db_path,
            )
            if issue.status and issue.status != existing.status:
                print(f"Moved {key} to {issue.status}")
        except AthenaError as exc:
            print(
                f"::warning::could not update {existing.name!r} "
                f"(created by {existing.author or 'unknown'!r}): {exc}"
            )
            task = existing
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
            title=issue.title,
            description=description,
            db_path=db_path,
            tasks_folder_id=tasks_folder_id,
            author=author,
            prefix=prefix,
            issue_number=issue_number,
            initial_status=issue.status,
        )
        print(
            f"::notice::Athena recorded the task author as "
            f"{task.author or '(empty)'!r} — this is the identity your "
            f"ATHENA_TOKEN authenticates as, and the only one allowed to "
            f"edit the task's tags and collaborators."
        )

    apply_collaborators(
        client,
        node_id=task.id,
        editors=editors,
        co_authors=co_authors,
        db_path=db_path,
    )
    task_url = client.create_shortlink(node_id=task.id, db_path=db_path)

    if existing is not None:
        print(f"Updated {key} ({task.id}) for issue #{issue_number}")
    else:
        url_suffix = f" — {task_url}" if task_url else ""
        post_issue_comment(
            repo,
            issue_number,
            f"Created Athena task **{key}**: {issue.title}{url_suffix}\n\n"
            f"Reference this key in PR titles (e.g. `{key}: <description>`) "
            f"to link future PRs to this task.",
        )
        add_issue_label(repo, issue_number, key, "1d76db")
        print(f"Created {key} ({task.id}) for issue #{issue_number}")

    return {
        "ticket_key": key,
        "ticket_number": str(issue_number),
        "task_id": task.id,
        "task_url": task_url or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Path to GitHub event JSON (defaults to $GITHUB_EVENT_PATH)")
    parser.add_argument(
        "--issue-number",
        help="Issue number(s) to backfill; comma-separated for several",
    )
    parser.add_argument("--issue-title", help="Override title for backfill mode")
    parser.add_argument("--issue-body", default="", help="Override body for backfill mode")
    parser.add_argument("--repo", required=True, help="owner/repo slug")
    args = parser.parse_args()

    project_uuid = os.environ.get("ATHENA_PROJECT_UUID", "")
    db_path = os.environ.get("ATHENA_DB_PATH", "")
    base_url = os.environ.get("ATHENA_BASE_URL", "")
    token = os.environ.get("ATHENA_TOKEN", "")
    folder_name = os.environ.get("ATHENA_TASKS_FOLDER", DEFAULT_TASKS_FOLDER)
    author = os.environ.get("ATHENA_AUTHOR", "").strip()
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

    # A backfilled issue is read from GitHub unless the caller supplied an
    # explicit title override.
    issues: list[GithubIssue] = []
    if args.issue_number:
        numbers = parse_issue_numbers(args.issue_number)
        if not numbers:
            print(
                f"::error::no usable issue numbers in {args.issue_number!r}",
                file=sys.stderr,
            )
            return 1
        if args.issue_title and len(numbers) > 1:
            print(
                "::warning::--issue-title/--issue-body apply to a single "
                "issue; ignoring them and reading each issue from GitHub"
            )
        for number in numbers:
            if args.issue_title and len(numbers) == 1:
                issues.append(
                    GithubIssue(
                        number=number, title=args.issue_title, body=args.issue_body
                    )
                )
            else:
                issues.append(fetch_issue(args.repo, number))
    else:
        issues.append(extract_issue_from_event(read_event(args.event)))

    if author:
        print(
            f"::warning::'author' is set to {author!r}. Athena stores that "
            f"string verbatim and then checks the *caller* against it, so "
            f"unless it is exactly the identity your ATHENA_TOKEN "
            f"authenticates as (bot tokens look like "
            f"'bot-<hex>-<name>@bots.local'), every tag and collaborator "
            f"write on the new task will be rejected with 403. Leave "
            f"'author' empty to let the server fill it in."
        )

    outputs: dict[str, str] = {}
    try:
        with AthenaClient(base_url=base_url, token=token) as client:
            tasks_folder_id = ensure_tasks_folder(
                client,
                db_path=db_path,
                folder_name=folder_name,
                author=author,
            )
            for issue in issues:
                outputs = sync_issue(
                    client,
                    repo=args.repo,
                    issue=issue,
                    prefix=prefix,
                    db_path=db_path,
                    tasks_folder_id=tasks_folder_id,
                    author=author,
                    editors=editors,
                    co_authors=co_authors,
                )
    except AthenaError as exc:
        print(f"::warning::Athena API error, skipping: {exc}")
        return 0

    if outputs:
        emit_output(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
