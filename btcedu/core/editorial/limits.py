"""Hard consumption limits for the unattended newsroom run.

Everything here exists because the daily run is the first part of the newsroom
that nobody watches. A single-story test could be capped by an operator sitting
in front of it; a timer cannot, so the limits have to be explicit, persistent
and checked *before* the call rather than regretted after it.

Three rules shape the design:

* **No unlimited default.** ``DailyLimits`` has no defaults at all. A caller
  that forgets a limit gets a ``TypeError``, not an unbounded bill.
* **The ledger outlives the process.** Counters live in SQLite, not in memory,
  so a crash-restart loop cannot spend the day's budget several times over.
* **Reserve before spending.** The conservative maximum cost of a call is
  charged against the budget first; the real cost replaces it afterwards. A
  reply that never arrives, or arrives and is then discarded, still counts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from btcedu.core.editorial.jobs import ProviderCallNotAttempted

BERLIN = ZoneInfo("Europe/Berlin")

#: Prices are only ever used to *over*-estimate before a call. Under-estimating
#: would let a run slip past its budget; over-estimating only stops it early.
_INPUT_USD_PER_TOKEN = 0.0000025
_OUTPUT_USD_PER_TOKEN = 0.000010

#: Added to the serialised payload to account for the system prompt and schema
#: the model service wraps around it, which the caller here cannot see.
_HIDDEN_PROMPT_CHARS = 12_000


class BudgetNotApproved(RuntimeError):
    """No daily budget was granted, so no paid call may be attempted."""


@dataclass(frozen=True)
class DailyLimits:
    """What one day of unattended operation is allowed to consume.

    No field has a default. The point of this object is to make an omission
    impossible to express, and a default of ``None`` meaning "unlimited" is
    exactly the omission it exists to prevent.
    """

    budget_usd: float
    max_calls: int
    max_stories: int

    def __post_init__(self) -> None:
        for name in ("budget_usd", "max_calls", "max_stories"):
            value = getattr(self, name)
            if value is None or value < 0:
                raise ValueError(f"{name} must be a non-negative number")

    @property
    def paid_calls_allowed(self) -> bool:
        """A zero on either axis is a deliberate, complete stop.

        This is the state the timer runs in until a daily budget is approved:
        installed, scheduled, doing all of its free work, and unable to spend.
        """
        return self.budget_usd > 0 and self.max_calls > 0

    def to_dict(self) -> dict:
        return {
            "budget_usd": self.budget_usd,
            "max_calls": self.max_calls,
            "max_stories": self.max_stories,
        }


def today_key(now: datetime | None = None) -> str:
    """The day boundary the operator experiences, not UTC's.

    The run is scheduled in Berlin time against a German evening broadcast. A
    UTC day key would move the reset into the middle of the evening for half
    the year.
    """
    moment = now or datetime.now(BERLIN)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BERLIN)
    return moment.astimezone(BERLIN).date().isoformat()


class DailyLedger:
    """Per-day call and cost counters that survive a restart.

    SQLite rather than a JSON file: a reservation is written and committed
    before the provider is contacted, so a process killed mid-call still has
    the money booked when it comes back.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL UNIQUE,
                    day TEXT NOT NULL,
                    task TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    actual_usd REAL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_daily_calls_day ON daily_calls(day)")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)

    def spent_usd(self, day: str) -> float:
        """What the day has cost, counting reservations still in flight.

        ``COALESCE(actual_usd, reserved_usd)`` is the whole point: a call whose
        reply was thrown away, or whose process died, is charged at the amount
        that was reserved for it. Charging only settled calls would make a
        crash loop free.
        """
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) "
                "FROM daily_calls WHERE day = ?",
                (day,),
            ).fetchone()
        return float(row[0])

    def call_count(self, day: str) -> int:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM daily_calls WHERE day = ?", (day,)
            ).fetchone()
        return int(row[0])

    def reserve(self, *, day: str, task: str, amount_usd: float) -> str:
        with closing(self._connect()) as db:
            call_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO daily_calls "
                "(call_id, day, task, reserved_usd, actual_usd, outcome, created_at) "
                "VALUES (?, ?, ?, ?, NULL, 'reserved', ?)",
                (call_id, day, task, float(amount_usd), datetime.now(BERLIN).isoformat()),
            )
        return call_id

    def settle(self, call_id: str, *, actual_usd: float, outcome: str = "completed") -> None:
        """Replace the reservation with the real cost.

        The reservation is never simply removed on failure: a provider that
        answered and was then rejected by validation has still been paid.
        """
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE daily_calls SET actual_usd = ?, outcome = ? WHERE call_id = ?",
                (float(actual_usd), outcome, call_id),
            )

    def abandon(self, call_id: str, *, outcome: str = "failed") -> None:
        """A call that produced no usable reply keeps its reserved amount.

        Whether the provider billed it is unknowable from here, and guessing
        "free" is the guess that can overspend.
        """
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE daily_calls SET outcome = ? WHERE call_id = ?", (outcome, call_id)
            )

    def summary(self, day: str) -> dict:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT task, outcome, COUNT(*), "
                "COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) "
                "FROM daily_calls WHERE day = ? GROUP BY task, outcome",
                (day,),
            ).fetchall()
        return {
            "day": day,
            "calls": self.call_count(day),
            "spent_usd": round(self.spent_usd(day), 6),
            "by_task": [
                {"task": task, "outcome": outcome, "calls": count, "usd": round(usd, 6)}
                for task, outcome, count, usd in rows
            ],
        }


class LedgerGuardedModel:
    """A model caller that cannot exceed the day's limits.

    Wraps the editorial model rather than replacing it, so the workflow below
    is unchanged and the guard is the only thing that had to be trusted.
    """

    def __init__(
        self,
        model,
        *,
        ledger: DailyLedger,
        limits: DailyLimits,
        max_tokens_by_task: dict[str, int],
        day: str | None = None,
    ) -> None:
        self.model = model
        self.ledger = ledger
        self.limits = limits
        self.max_tokens_by_task = max_tokens_by_task
        self.day = day or today_key()
        self._failed_payloads: set[str] = set()

    def maximum_cost_usd(self, payload: dict) -> float:
        task = payload.get("task", "")
        max_tokens = self.max_tokens_by_task.get(task)
        if max_tokens is None:
            raise ProviderCallNotAttempted(
                f"Refusing {task!r}: no token ceiling configured, so its cost is unbounded"
            )
        chars = len(json.dumps(payload, ensure_ascii=False)) + _HIDDEN_PROMPT_CHARS
        input_tokens = (chars + 1) // 2
        return input_tokens * _INPUT_USD_PER_TOKEN + max_tokens * _OUTPUT_USD_PER_TOKEN

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def __call__(self, payload: dict):
        if not self.limits.paid_calls_allowed:
            raise BudgetNotApproved(
                "No daily budget is approved: "
                f"budget {self.limits.budget_usd} USD, {self.limits.max_calls} calls"
            )
        fingerprint = self._fingerprint(payload)
        if fingerprint in self._failed_payloads:
            raise ProviderCallNotAttempted(
                "Refusing to resend an identical payload that already failed; "
                "the input has to change first"
            )
        maximum = self.maximum_cost_usd(payload)
        spent = self.ledger.spent_usd(self.day)
        if spent + maximum > self.limits.budget_usd:
            raise ProviderCallNotAttempted(
                f"Daily budget guard blocked {payload.get('task')!r}: "
                f"{spent:.4f} spent + {maximum:.4f} maximum > "
                f"{self.limits.budget_usd:.4f} USD"
            )
        if self.ledger.call_count(self.day) >= self.limits.max_calls:
            raise ProviderCallNotAttempted(
                f"Daily call guard blocked {payload.get('task')!r}: "
                f"{self.limits.max_calls} calls already made"
            )
        call_id = self.ledger.reserve(
            day=self.day, task=str(payload.get("task", "unknown")), amount_usd=maximum
        )
        try:
            reply = self.model(payload)
        except Exception:
            self.ledger.abandon(call_id)
            self._failed_payloads.add(fingerprint)
            raise
        self.ledger.settle(call_id, actual_usd=float(getattr(reply, "cost_usd", 0.0) or 0.0))
        return reply
