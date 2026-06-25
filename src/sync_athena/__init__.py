"""sync-athena: bidirectional sync between GitHub and Athena.

Public surface intentionally tiny — the workflow only needs:

    from sync_athena import AthenaClient, markdown_to_blocknote
    from sync_athena.counter import (
        create_ticket,
        next_ticket_number,
        extract_ticket_number,
        hashtag_for_prefix,
    )

Everything else (HTTP details, retry policy, response parsing) stays
private so callers don't depend on the Athena REST shape.
"""

from sync_athena.athena_client import AthenaClient, AthenaError
from sync_athena.markdown_to_blocknote import markdown_to_blocknote

__all__ = [
    "AthenaClient",
    "AthenaError",
    "markdown_to_blocknote",
]