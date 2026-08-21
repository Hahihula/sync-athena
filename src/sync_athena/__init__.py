"""sync-athena: bidirectional sync between GitHub and Athena.

Public surface intentionally tiny — the workflow only needs:

    from sync_athena import AthenaClient, markdown_to_blocknote
    from sync_athena.tickets import (
        create_ticket,
        find_task_for_issue,
        issue_number_from_url,
        ticket_key,
    )
    from sync_athena.collaborators import apply_collaborators, parse_list

Everything else (HTTP details, response parsing) stays private so callers
don't depend on the Athena REST shape.
"""

from sync_athena.athena_client import AthenaClient, AthenaError, Collaborators, Node
from sync_athena.markdown_to_blocknote import markdown_to_blocknote

__all__ = [
    "AthenaClient",
    "AthenaError",
    "Collaborators",
    "Node",
    "markdown_to_blocknote",
]