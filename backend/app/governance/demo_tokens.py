"""Readable, non-secret aliases for the fixed NEXUS demonstration identities.

The canonical tokens (``4``, ``D02`` ... ``I12``) remain defined by the 7A
dataset and stored in PostgreSQL. These aliases merely select an *existing*
database identity; they never assign a role, project, source, or permission.
They are consulted only from the demo-mode-gated identity resolver.
"""
from __future__ import annotations

from types import MappingProxyType


# Each suffix reflects the employee's *initial* 7A project assignment. A
# project's current authorization is still resolved afresh from Postgres.
DEMO_TOKEN_ALIASES = MappingProxyType({
    "dev-02-pay": "D02",
    "dev-03-pay": "D03",
    "dev-04-com": "D04",
    "dev-05-com": "D05",
    "dev-06-id": "D06",
    "dev-07-id": "D07",
    "dev-08-data": "D08",
    "dev-09-data": "D09",
    "intern-10-com": "I10",
    "intern-11-data": "I11",
    "intern-12-id": "I12",
})


def canonical_demo_token(token: str) -> str:
    """Translate an exact demo alias to its seeded token; never parse scope."""
    return DEMO_TOKEN_ALIASES.get(token, token)
