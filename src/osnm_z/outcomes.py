"""Submission and confirmation outcomes for one mint transaction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .rpc_protocol import RawTransactionSubmission, SubmissionDisposition, TransactionReceipt


class ExecutionState(Enum):
    """Operator-visible terminal state for one requested execution."""

    POSSIBLY_SUBMITTED = "possibly_submitted"
    SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
    MINED_REVERTED = "mined_reverted"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class TransactionExecutionResult:
    """Submission disposition and optional receipt for one signed transaction."""

    submission: RawTransactionSubmission
    receipt: TransactionReceipt | None

    @property
    def state(self) -> ExecutionState:
        if self.receipt is not None:
            return (
                ExecutionState.COMPLETED
                if self.receipt.is_success
                else ExecutionState.MINED_REVERTED
            )
        if self.submission.disposition is SubmissionDisposition.POSSIBLY_SUBMITTED:
            return ExecutionState.POSSIBLY_SUBMITTED
        return ExecutionState.SUBMITTED_UNCONFIRMED
