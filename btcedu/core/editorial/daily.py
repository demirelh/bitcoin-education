"""The unattended daily run: transcript in, Turkish drafts on the dev site.

This ties the existing newsroom together rather than reimplementing it. The
broadcast stories come from the video pipeline's ``stories.json``, each one
goes through ``draft_story`` — research, claims, evidence, Turkish article,
licensed picture — and the result is offered on the protected development
site under the approved auto-release switch.

What this module adds is everything a timer needs and a person does not:

* a lock, so two runs cannot interleave on the same database,
* a durable record of which stories are done, so a restart resumes instead of
  paying again,
* per-story isolation, so one failure does not cost the other stories,
* a status file precise enough to distinguish "no transcript" from "no budget"
  from "it broke".
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import traceback
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from btcedu.core.editorial.limits import (
    BERLIN,
    BudgetNotApproved,
    DailyLedger,
    DailyLimits,
    LedgerGuardedModel,
    today_key,
)
from btcedu.core.editorial.transcript_source import (
    SelectedStory,
    broadcast_datetime,
    discover_transcripts,
    select_stories,
)

logger = logging.getLogger(__name__)


class RunOutcome(str, Enum):
    """The states an operator has to be able to tell apart at a glance."""

    SUCCESS = "success"
    NO_NEW_TRANSCRIPT = "no_new_transcript"
    NO_SUITABLE_TOPICS = "no_suitable_topics"
    BUDGET_NOT_APPROVED = "budget_not_approved"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    ALREADY_RUNNING = "already_running"


@dataclass
class StoryResult:
    key: str
    headline_de: str
    section: str
    status: str
    title_tr: str = ""
    url: str = ""
    detail: str = ""


@dataclass
class RunReport:
    outcome: str
    started_at: str
    finished_at: str = ""
    day: str = ""
    transcript: str = ""
    broadcast_date: str = ""
    attribution: str = ""
    limits: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    stories: list = field(default_factory=list)
    published: int = 0
    errors: list = field(default_factory=list)
    next_run: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["stories"] = [
            asdict(item) if isinstance(item, StoryResult) else item for item in self.stories
        ]
        return payload


class RunLock:
    """One run at a time, released even if the process is killed.

    An advisory lock file with the pid in it, taken with ``O_EXCL``. A stale
    file from a machine that lost power is detected by checking the pid rather
    than by trusting a timestamp, because a reboot can happen in a second.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._holder_alive():
            logger.warning("Removing stale run lock at %s", self.path)
            self.path.unlink(missing_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise AlreadyRunning(f"Another run holds {self.path}") from exc
        os.write(self._fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)

    def _holder_alive(self) -> bool:
        try:
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class AlreadyRunning(RuntimeError):
    """Another daily run holds the lock."""


class ProcessedStories:
    """Which broadcast stories the newsroom has already handled.

    Separate from the newsroom database on purpose. A story that was attempted
    and failed for a reason that will not change — no usable source, no
    evidence — must not be retried every single day at full price, and that is
    a fact about the *run*, not about any article that exists.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_stories (
                    story_key TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def status(self, story_key: str) -> tuple[str, int]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT status, attempts FROM processed_stories WHERE story_key = ?",
                (story_key,),
            ).fetchone()
        return (row[0], int(row[1])) if row else ("", 0)

    def record(self, story_key: str, *, episode_id: str, status: str, detail: str = "") -> None:
        now = datetime.now(BERLIN).isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """
                INSERT INTO processed_stories
                    (story_key, episode_id, status, detail, attempts, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(story_key) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    attempts = processed_stories.attempts + 1,
                    updated_at = excluded.updated_at
                """,
                (story_key, episode_id, status, detail[:500], now),
            )
            db.commit()

    def counts(self) -> dict:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM processed_stories GROUP BY status"
            ).fetchall()
        return {status: count for status, count in rows}


#: A story that failed this many times is left alone. Retrying a deterministic
#: rejection forever is the most expensive possible way to stay broken.
MAX_STORY_ATTEMPTS = 2

#: Statuses that mean "do not touch this story again".
TERMINAL_STATUSES = frozenset({"published", "drafted"})


def pending_stories(
    selected: list[SelectedStory], processed: ProcessedStories
) -> list[SelectedStory]:
    """What is left to do, so a rerun is cheap and produces no duplicates."""
    pending: list[SelectedStory] = []
    for item in selected:
        status, attempts = processed.status(item.key)
        if status in TERMINAL_STATUSES:
            continue
        if attempts >= MAX_STORY_ATTEMPTS:
            continue
        pending.append(item)
    return pending


def write_report(path: Path, report: RunReport) -> None:
    """Atomically, because the status page reads this while runs happen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def load_report(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class DailyPaths:
    """Everything the run writes. All of it inside the development directory."""

    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "current-news.sqlite"

    @property
    def ledger(self) -> Path:
        return self.data_dir / "daily-ledger.sqlite"

    @property
    def processed(self) -> Path:
        return self.data_dir / "daily-processed.sqlite"

    @property
    def lock(self) -> Path:
        return self.data_dir / "daily-run.lock"

    @property
    def report(self) -> Path:
        return self.data_dir / "daily-status.json"

    @property
    def private(self) -> Path:
        return self.data_dir / "current-private"

    @property
    def site(self) -> Path:
        return self.data_dir / "site"


def story_failure(exc: BaseException) -> str:
    """One line an operator can act on, not a wall of stack."""
    return f"{type(exc).__name__}: {exc}".strip()[:400]


def run_daily(
    *,
    paths: DailyPaths,
    limits: DailyLimits,
    outputs_dir: Path,
    settings_factory,
    model_factory,
    drafter,
    publisher,
    lookback_days: int = 3,
    today: date | None = None,
    now: datetime | None = None,
) -> RunReport:
    """One unattended pass. Never raises for a single story's sake.

    The callables are injected so the whole run is exercisable offline: the
    tests supply fixtures where production supplies a paid model and the real
    search and Commons providers. Nothing about the control flow changes
    between the two, which is the only way the offline test is worth anything.
    """
    moment = now or datetime.now(BERLIN)
    day = today_key(moment)
    report = RunReport(
        outcome=RunOutcome.ERROR.value,
        started_at=moment.isoformat(),
        day=day,
        limits=limits.to_dict(),
    )
    ledger = DailyLedger(paths.ledger)
    processed = ProcessedStories(paths.processed)

    try:
        with RunLock(paths.lock):
            sources = discover_transcripts(
                Path(outputs_dir), lookback_days=lookback_days, today=today or moment.date()
            )
            if not sources:
                report.outcome = RunOutcome.NO_NEW_TRANSCRIPT.value
                return _finish(report, ledger, day, paths, processed, moment)

            candidates: list[SelectedStory] = []
            chosen = sources[0]
            for source in sources:
                available = pending_stories(
                    select_stories(source, limit=limits.max_stories), processed
                )
                if available:
                    chosen, candidates = source, available
                    break
            report.transcript = chosen.episode_id
            report.broadcast_date = chosen.broadcast_date.isoformat()
            report.attribution = chosen.attribution

            if not candidates:
                report.outcome = RunOutcome.NO_SUITABLE_TOPICS.value
                return _finish(report, ledger, day, paths, processed, moment)

            if not limits.paid_calls_allowed:
                report.outcome = RunOutcome.BUDGET_NOT_APPROVED.value
                report.stories = [
                    StoryResult(
                        key=item.key,
                        headline_de=item.story.headline_de,
                        section=item.section,
                        status="waiting_for_budget",
                    )
                    for item in candidates
                ]
                return _finish(report, ledger, day, paths, processed, moment)

            settings = settings_factory(paths)
            model = LedgerGuardedModel(
                model_factory(settings),
                ledger=ledger,
                limits=limits,
                max_tokens_by_task=MAX_TOKENS_BY_TASK,
                day=day,
            )
            exhausted = False
            for item in candidates:
                if exhausted:
                    break
                try:
                    result = drafter(
                        settings=settings,
                        model=model,
                        selected=item,
                        published_at=broadcast_datetime(chosen),
                    )
                except BudgetNotApproved as exc:
                    report.outcome = RunOutcome.BUDGET_NOT_APPROVED.value
                    report.errors.append(story_failure(exc))
                    exhausted = True
                    break
                except Exception as exc:  # noqa: BLE001 - isolated per story
                    detail = story_failure(exc)
                    logger.warning("Story %s failed: %s", item.key, detail)
                    logger.debug("%s", traceback.format_exc(limit=20))
                    processed.record(
                        item.key, episode_id=item.episode_id, status="failed", detail=detail
                    )
                    report.stories.append(
                        StoryResult(
                            key=item.key,
                            headline_de=item.story.headline_de,
                            section=item.section,
                            status="failed",
                            detail=detail,
                        )
                    )
                    if _is_exhaustion(detail):
                        report.outcome = RunOutcome.BUDGET_EXHAUSTED.value
                        exhausted = True
                    continue
                processed.record(
                    item.key, episode_id=item.episode_id, status="drafted", detail=result.title
                )
                report.stories.append(
                    StoryResult(
                        key=item.key,
                        headline_de=item.story.headline_de,
                        section=item.section,
                        status="drafted",
                        title_tr=result.title,
                    )
                )

            offered = publisher(settings=settings, paths=paths)
            report.published = offered.get("articles", 0)
            for entry in report.stories:
                if entry.status == "drafted":
                    entry.url = offered.get("urls", {}).get(entry.title_tr, "")
            if report.outcome not in {
                RunOutcome.BUDGET_EXHAUSTED.value,
                RunOutcome.BUDGET_NOT_APPROVED.value,
            }:
                report.outcome = (
                    RunOutcome.SUCCESS.value
                    if any(entry.status == "drafted" for entry in report.stories)
                    else RunOutcome.ERROR.value
                )
                if not report.stories:
                    report.outcome = RunOutcome.NO_SUITABLE_TOPICS.value
            return _finish(report, ledger, day, paths, processed, moment)
    except AlreadyRunning as exc:
        report.outcome = RunOutcome.ALREADY_RUNNING.value
        report.errors.append(story_failure(exc))
        report.finished_at = datetime.now(BERLIN).isoformat()
        return report
    except Exception as exc:  # noqa: BLE001 - a run must always leave a status
        report.outcome = RunOutcome.ERROR.value
        report.errors.append(story_failure(exc))
        logger.exception("Daily newsroom run failed")
        return _finish(report, ledger, day, paths, processed, moment)


def _is_exhaustion(detail: str) -> bool:
    lowered = detail.casefold()
    return "budget guard" in lowered or "call guard" in lowered


def _finish(
    report: RunReport,
    ledger: DailyLedger,
    day: str,
    paths: DailyPaths,
    processed: ProcessedStories,
    moment: datetime,
) -> RunReport:
    report.finished_at = datetime.now(BERLIN).isoformat()
    report.usage = ledger.summary(day)
    report.usage["processed_total"] = processed.counts()
    write_report(paths.report, report)
    return report


#: Output ceilings per editorial task. A task without one is refused outright
#: by the guard, because an unbounded reply is an unbounded bill.
MAX_TOKENS_BY_TASK = {
    "extract_claims": 1400,
    "evaluate_claim_evidence": 1200,
    "draft_article": 1800,
    "check_article_consistency": 600,
}
