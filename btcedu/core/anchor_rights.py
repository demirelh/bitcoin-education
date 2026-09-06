"""The presenter's release, as a machine-readable record — and nothing more.

A synthetic presenter is somebody's face and somebody's voice. Before a single
frame is generated the pipeline has to be able to answer, mechanically: is there
consent, is it still valid, does it cover this channel and this territory, has
it been withdrawn, and is the AI disclosure configured.

What this module deliberately does *not* do is hold the paperwork. The contract
lives wherever contracts live at the operator's organisation; the repository
gets a record with the *status* of that contract and an internal reference to
it. Unknown keys are rejected rather than ignored, which is what keeps a
well-meaning "let's also note her address here" from ever reaching git.

The file itself belongs outside the working tree — ``.gitignore`` covers the
production filename, and only ``anchor-rights.example.json`` is committed.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RIGHTS_FILENAME = "anchor-rights.json"
EXAMPLE_RIGHTS_FILENAME = "anchor-rights.example.json"

# Exactly the keys a record may carry. Anything else is refused: the schema is
# the data-minimisation control, not a comment asking for restraint.
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "record_version",
        "updated_at",
        "presenter_rights_id",
        "consent_confirmed",
        "voice_likeness_confirmed",
        "synthetic_video_confirmed",
        "permitted_channels",
        "permitted_territories",
        "valid_from",
        "valid_until",
        "revoked",
        "revoked_on",
        "contract_reference",
        "operator_approval",
        "ai_disclosure_text",
    }
)
_ALLOWED_APPROVAL_KEYS = frozenset({"approved", "approved_by_ref", "approved_on"})

# An operator reference is an initial, a role or a ticket id. The pattern is
# narrow enough that an email address or a full name will not fit through it.
_OPERATOR_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")


class RightsRecordError(ValueError):
    """The rights record is malformed, or carries data it must not carry."""


@dataclass(frozen=True)
class OperatorApproval:
    approved: bool = False
    approved_by_ref: str = ""
    approved_on: date | None = None


@dataclass(frozen=True)
class RightsRecord:
    """The status of one presenter's release. Never the release itself."""

    schema_version: int
    presenter_rights_id: str
    consent_confirmed: bool
    voice_likeness_confirmed: bool
    synthetic_video_confirmed: bool
    permitted_channels: tuple[str, ...]
    permitted_territories: tuple[str, ...]
    valid_from: date
    contract_reference: str
    ai_disclosure_text: str
    record_version: int = 1
    updated_at: str = ""
    valid_until: date | None = None
    revoked: bool = False
    revoked_on: date | None = None
    operator_approval: OperatorApproval = field(default_factory=OperatorApproval)
    source_path: Path | None = field(default=None, compare=False)

    def covers_channel(self, channel: str) -> bool:
        wanted = (channel or "").strip().lower()
        return any(entry.strip().lower() == wanted for entry in self.permitted_channels)

    def covers_territory(self, territory: str) -> bool:
        wanted = (territory or "").strip().lower()
        return any(
            entry.strip().lower() in {wanted, "worldwide"} for entry in self.permitted_territories
        )


def _as_bool(value, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise RightsRecordError(f"Rights record field {key!r} must be true or false")


def _as_date(value, key: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise RightsRecordError(f"Rights record field {key!r} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RightsRecordError(
            f"Rights record field {key!r} is not an ISO date: {value!r}"
        ) from exc


def _as_str_tuple(value, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RightsRecordError(f"Rights record field {key!r} must be a list of strings")
    entries = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RightsRecordError(f"Rights record field {key!r} contains a non-string entry")
        entries.append(item.strip())
    return tuple(entries)


def _parse_approval(raw) -> OperatorApproval:
    if raw is None:
        return OperatorApproval()
    if not isinstance(raw, dict):
        raise RightsRecordError("Rights record field 'operator_approval' must be an object")
    unknown = sorted(set(raw) - _ALLOWED_APPROVAL_KEYS)
    if unknown:
        raise RightsRecordError(f"Unsupported keys in operator_approval: {unknown}")
    approved = _as_bool(raw.get("approved", False), "operator_approval.approved")
    by_ref = str(raw.get("approved_by_ref") or "").strip()
    if by_ref and not _OPERATOR_REF.match(by_ref):
        raise RightsRecordError(
            "operator_approval.approved_by_ref must be a short non-personal reference "
            "such as an initial or a ticket id"
        )
    approved_on = raw.get("approved_on")
    return OperatorApproval(
        approved=approved,
        approved_by_ref=by_ref,
        approved_on=_as_date(approved_on, "operator_approval.approved_on")
        if approved_on
        else None,
    )


def parse_rights_record(data: dict, *, source_path: Path | None = None) -> RightsRecord:
    """Validate a rights record, refusing anything outside the schema."""
    if not isinstance(data, dict):
        raise RightsRecordError("Rights record must be a JSON object")

    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise RightsRecordError(
            "Rights record carries keys that are not part of the schema: "
            f"{unknown}. Personal data and contract text must not be stored here."
        )

    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise RightsRecordError(
            f"Unsupported rights record schema_version: {schema_version!r} "
            f"(expected {SCHEMA_VERSION})"
        )

    presenter_id = str(data.get("presenter_rights_id") or "").strip()
    if not presenter_id:
        raise RightsRecordError("Rights record is missing 'presenter_rights_id'")
    if not _OPERATOR_REF.match(presenter_id):
        raise RightsRecordError(
            "presenter_rights_id must be a pseudonymous identifier, not a name or contact detail"
        )

    contract_reference = str(data.get("contract_reference") or "").strip()
    if not contract_reference:
        raise RightsRecordError(
            "Rights record is missing 'contract_reference' — the internal pointer to "
            "the contract that is stored outside this repository"
        )

    valid_until = data.get("valid_until")
    revoked_on = data.get("revoked_on")
    record = RightsRecord(
        schema_version=schema_version,
        presenter_rights_id=presenter_id,
        consent_confirmed=_as_bool(data.get("consent_confirmed", False), "consent_confirmed"),
        voice_likeness_confirmed=_as_bool(
            data.get("voice_likeness_confirmed", False), "voice_likeness_confirmed"
        ),
        synthetic_video_confirmed=_as_bool(
            data.get("synthetic_video_confirmed", False), "synthetic_video_confirmed"
        ),
        permitted_channels=_as_str_tuple(data.get("permitted_channels", []), "permitted_channels"),
        permitted_territories=_as_str_tuple(
            data.get("permitted_territories", []), "permitted_territories"
        ),
        valid_from=_as_date(data.get("valid_from"), "valid_from"),
        valid_until=_as_date(valid_until, "valid_until") if valid_until else None,
        revoked=_as_bool(data.get("revoked", False), "revoked"),
        revoked_on=_as_date(revoked_on, "revoked_on") if revoked_on else None,
        contract_reference=contract_reference,
        operator_approval=_parse_approval(data.get("operator_approval")),
        ai_disclosure_text=str(data.get("ai_disclosure_text") or "").strip(),
        record_version=int(data.get("record_version", 1)),
        updated_at=str(data.get("updated_at") or "").strip(),
        source_path=Path(source_path) if source_path else None,
    )

    if record.valid_until and record.valid_until < record.valid_from:
        raise RightsRecordError("Rights record valid_until is before valid_from")
    return record


def load_rights_record(path: str | Path) -> RightsRecord:
    file_path = Path(path)
    if not file_path.is_file():
        raise RightsRecordError(f"Rights record not found: {file_path}")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RightsRecordError(f"Rights record is not valid JSON: {file_path}") from exc
    return parse_rights_record(data, source_path=file_path)


def rights_problems(
    record: RightsRecord,
    *,
    channel: str,
    territory: str,
    today: date | None = None,
) -> list[str]:
    """Everything that blocks generation under this release.

    A list rather than an exception: an operator chasing a release wants to see
    all of it at once, not to rediscover the next missing signature on every run.
    """
    today = today or datetime.now(UTC).date()
    problems: list[str] = []

    if record.revoked:
        on = f" on {record.revoked_on.isoformat()}" if record.revoked_on else ""
        problems.append(f"Consent was revoked{on}: no further generation is permitted")
    if not record.consent_confirmed:
        problems.append("consent_confirmed is false: the presenter has not agreed")
    if not record.voice_likeness_confirmed:
        problems.append("voice_likeness_confirmed is false: voice and likeness use is not covered")
    if not record.synthetic_video_confirmed:
        problems.append(
            "synthetic_video_confirmed is false: synthetic video generation is not covered"
        )
    if today < record.valid_from:
        problems.append(f"Release is not valid before {record.valid_from.isoformat()}")
    if record.valid_until and today > record.valid_until:
        problems.append(f"Release expired on {record.valid_until.isoformat()}")
    if channel and not record.covers_channel(channel):
        problems.append(f"Channel {channel!r} is not among the permitted channels")
    if territory and not record.covers_territory(territory):
        problems.append(f"Territory {territory!r} is not among the permitted territories")
    if not record.operator_approval.approved:
        problems.append("operator_approval.approved is false: the operator has not signed off")
    elif not record.operator_approval.approved_by_ref:
        problems.append("operator_approval is missing a non-personal approved_by_ref")
    if not record.ai_disclosure_text:
        problems.append(
            "ai_disclosure_text is empty: the AI disclosure shown with the video is not configured"
        )
    return problems
