"""Authenticated client for temporary Stage 12 persistence operations."""

from policyengine_stage12_client.client import Stage12PersistenceClient
from policyengine_stage12_client.identity import (
    STAGE12_PERSISTENCE_AUDIENCE,
    GoogleIdentityTokenProvider,
)

__all__ = [
    "STAGE12_PERSISTENCE_AUDIENCE",
    "GoogleIdentityTokenProvider",
    "Stage12PersistenceClient",
]
