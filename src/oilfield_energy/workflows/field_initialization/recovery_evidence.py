"""Immutable numerical admission and original-state guard for explicit recovery."""

from pydantic import AwareDatetime

from .contracts import InitializationEvidence, ObservationCursor, PccTargetEvidence
from .contracts_recovery import PrepareResynchronizationRequest
from .contracts_values import Value


class ResynchronizationEvidence(Value):
    request: PrepareResynchronizationRequest
    base_fingerprint: str
    inputs: InitializationEvidence
    target: PccTargetEvidence | None
    expired_target_id: str | None = None
    cursor: ObservationCursor
    expires_at: AwareDatetime
