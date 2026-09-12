"""Resolving provider operations whose outcome is actually known.

A ``reconcile_required`` operation blocks every later retry, and rightly so:
retrying a paid call whose outcome is unknown risks paying twice for an effect
that may already exist. But nothing in the editorial code could ever clear that
state, so a single failed search left its story permanently dead -- which an
unattended daily run cannot recover from.

This module resolves only the cases where the outcome is *not* in doubt: an
operation that recorded no result and cost nothing had no effect, so retrying
it can neither duplicate work nor duplicate a bill. Everything else stays
blocked and is reported for an operator. The point is to remove an unnecessary
dead end, not to soften the guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from btcedu.models.editorial import (
    ProviderOperation,
    ProviderOperationStatus,
    ResearchQuery,
    ResearchQueryStatus,
)

#: Operation types whose failure leaves nothing behind to pay for or undo.
#: Search over the key-less endpoints is free and has no side effect; a model
#: call is neither, which is why it is deliberately absent.
_EFFECT_FREE_TYPES = frozenset({"search"})


@dataclass
class ReconcileReport:
    resolved: list[str] = field(default_factory=list)
    requires_operator: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.resolved)

    def to_dict(self) -> dict:
        return {
            "resolved": list(self.resolved),
            "requires_operator": list(self.requires_operator),
        }


def _is_effect_free(operation: ProviderOperation) -> bool:
    if operation.operation_type not in _EFFECT_FREE_TYPES:
        return False
    # A charge, even an estimated one, means the bill is not known to be zero.
    if (operation.actual_cost_usd or 0.0) > 0.0:
        return False
    return (operation.estimated_cost_usd or 0.0) <= 0.0


def reconcile_operations(
    session: Session, *, research_run_id: int | None = None
) -> ReconcileReport:
    """Clear operations that demonstrably had no effect and no cost.

    ``submitted`` operations are never touched: their outcome is genuinely
    unknown, and that is the case the guard exists for.
    """
    statement = select(ProviderOperation).where(
        ProviderOperation.status == ProviderOperationStatus.RECONCILE_REQUIRED.value
    )
    if research_run_id is not None:
        statement = statement.where(ProviderOperation.research_run_id == research_run_id)

    report = ReconcileReport()
    for operation in session.execute(statement).scalars():
        if not _is_effect_free(operation):
            report.requires_operator.append(operation.operation_id)
            continue

        operation.status = ProviderOperationStatus.FAILED.value
        queries = (
            session.execute(
                select(ResearchQuery).where(ResearchQuery.provider_operation_id == operation.id)
            )
            .scalars()
            .all()
        )
        for query in queries:
            # The result was never durable, so nothing is thrown away here.
            if query.status == ResearchQueryStatus.BLOCKED.value:
                query.status = ResearchQueryStatus.FAILED.value
        report.resolved.append(operation.operation_id)

    if report.changed:
        session.commit()
    return report
