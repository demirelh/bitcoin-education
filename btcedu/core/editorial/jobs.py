"""Persistent reservation helpers for editorial provider work."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from btcedu.models.editorial import (
    ProviderOperation,
    ProviderOperationStatus,
    ResearchRun,
)


class EditorialBudgetExceeded(RuntimeError):
    pass


class EditorialOperationConflict(RuntimeError):
    pass


_BUDGETED_STATUSES = frozenset(
    {
        ProviderOperationStatus.RESERVED.value,
        ProviderOperationStatus.SUBMITTED.value,
        ProviderOperationStatus.COMPLETED.value,
        ProviderOperationStatus.RECONCILE_REQUIRED.value,
    }
)


def reserve_research_run(
    session: Session,
    *,
    topic_id: int,
    input_hash: str,
    policy_version: str,
    model_name: str,
    max_queries: int,
    max_cost_usd: float,
) -> ResearchRun:
    if max_queries < 0:
        raise ValueError("max_queries must not be negative")
    if max_cost_usd < 0:
        raise ValueError("max_cost_usd must not be negative")

    existing = (
        session.query(ResearchRun)
        .filter_by(
            topic_id=topic_id,
            input_hash=input_hash,
            policy_version=policy_version,
            model_name=model_name,
        )
        .first()
    )
    if existing is not None:
        return existing

    run = ResearchRun(
        run_id=str(uuid.uuid4()),
        topic_id=topic_id,
        input_hash=input_hash,
        policy_version=policy_version,
        model_name=model_name,
        max_queries=max_queries,
        max_cost_usd=max_cost_usd,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return (
            session.query(ResearchRun)
            .filter_by(
                topic_id=topic_id,
                input_hash=input_hash,
                policy_version=policy_version,
                model_name=model_name,
            )
            .one()
        )
    return run


def reserved_cost_usd(session: Session, research_run_id: int) -> float:
    rows = (
        session.query(
            ProviderOperation.status,
            ProviderOperation.estimated_cost_usd,
            ProviderOperation.actual_cost_usd,
        )
        .filter(ProviderOperation.research_run_id == research_run_id)
        .all()
    )
    return float(
        sum(
            actual if actual is not None else estimate
            for status, estimate, actual in rows
            if status in _BUDGETED_STATUSES
        )
    )


def _begin_reservation(session: Session) -> None:
    """Serialize SQLite budget checks without holding a lock across provider I/O."""
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("Commit pending changes before reserving provider work")
    session.commit()
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _operation_matches(
    operation: ProviderOperation,
    *,
    operation_type: str,
    provider: str,
    model_name: str,
    input_hash: str,
) -> bool:
    return (
        operation.operation_type == operation_type
        and operation.provider == provider
        and operation.model_name == model_name
        and operation.input_hash == input_hash
    )


def _used_queries(session: Session, research_run_id: int) -> int:
    return (
        session.query(ProviderOperation)
        .filter(
            ProviderOperation.research_run_id == research_run_id,
            ProviderOperation.operation_type == "search",
            ProviderOperation.status.in_(_BUDGETED_STATUSES),
        )
        .count()
    )


def reserve_provider_operation(
    session: Session,
    *,
    research_run: ResearchRun,
    operation_key: str,
    operation_type: str,
    provider: str,
    model_name: str,
    input_hash: str,
    estimated_cost_usd: float,
) -> ProviderOperation:
    if estimated_cost_usd < 0:
        raise ValueError("estimated_cost_usd must not be negative")

    research_run_id = research_run.id
    max_cost_usd = research_run.max_cost_usd
    max_queries = research_run.max_queries
    _begin_reservation(session)
    try:
        existing = (
            session.query(ProviderOperation)
            .filter_by(research_run_id=research_run_id, operation_key=operation_key)
            .first()
        )
        if existing is not None:
            if not _operation_matches(
                existing,
                operation_type=operation_type,
                provider=provider,
                model_name=model_name,
                input_hash=input_hash,
            ):
                raise EditorialOperationConflict(
                    f"Operation key {operation_key!r} already identifies different work"
                )
            session.commit()
            return existing

        committed = reserved_cost_usd(session, research_run_id)
        if committed + estimated_cost_usd > max_cost_usd:
            raise EditorialBudgetExceeded(
                f"Research run budget exceeded: {committed + estimated_cost_usd:.4f} "
                f"> {max_cost_usd:.4f} USD"
            )

        used_queries = _used_queries(session, research_run_id)
        if operation_type == "search" and used_queries >= max_queries:
            raise EditorialBudgetExceeded(
                f"Research run query budget exceeded: {used_queries + 1} > {max_queries}"
            )

        operation = ProviderOperation(
            operation_id=str(uuid.uuid4()),
            research_run_id=research_run_id,
            operation_key=operation_key,
            operation_type=operation_type,
            provider=provider,
            model_name=model_name,
            input_hash=input_hash,
            estimated_cost_usd=estimated_cost_usd,
        )
        session.add(operation)
        research_run = session.get(ResearchRun, research_run_id)
        if research_run is None:
            raise RuntimeError(f"Research run disappeared during reservation: {research_run_id}")
        research_run.used_queries = used_queries + int(operation_type == "search")
        session.commit()
        return operation
    except (EditorialBudgetExceeded, EditorialOperationConflict):
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        existing = (
            session.query(ProviderOperation)
            .filter_by(research_run_id=research_run_id, operation_key=operation_key)
            .one()
        )
        if not _operation_matches(
            existing,
            operation_type=operation_type,
            provider=provider,
            model_name=model_name,
            input_hash=input_hash,
        ):
            raise EditorialOperationConflict(
                f"Operation key {operation_key!r} already identifies different work"
            )
        return existing
