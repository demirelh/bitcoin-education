"""The release record: strict schema, data minimisation, fail-closed evaluation."""

import json
from datetime import date

import pytest

from btcedu.core.anchor_rights import (
    SCHEMA_VERSION,
    RightsRecordError,
    load_rights_record,
    parse_rights_record,
    rights_problems,
)

VALID = {
    "schema_version": 1,
    "record_version": 3,
    "updated_at": "2026-01-31T12:00:00Z",
    "presenter_rights_id": "presenter-01",
    "consent_confirmed": True,
    "voice_likeness_confirmed": True,
    "synthetic_video_confirmed": True,
    "permitted_channels": ["almanya24-youtube"],
    "permitted_territories": ["DE", "TR"],
    "valid_from": "2026-01-01",
    "valid_until": "2030-01-01",
    "revoked": False,
    "contract_reference": "CONTRACT-1",
    "operator_approval": {
        "approved": True,
        "approved_by_ref": "ops-hd",
        "approved_on": "2026-01-01",
    },
    "ai_disclosure_text": "Sunucu yapay zeka ile olusturulmustur.",
}


def _record(**overrides):
    data = dict(VALID)
    data.update(overrides)
    return parse_rights_record(data)


class TestTheSchemaKeepsPersonalDataOut:
    @pytest.mark.parametrize(
        "extra_key",
        ["presenter_name", "email", "address", "date_of_birth", "contract_text", "notes"],
    )
    def test_an_unknown_key_is_refused(self, extra_key):
        data = dict(VALID)
        data[extra_key] = "anything at all"

        with pytest.raises(RightsRecordError) as exc:
            parse_rights_record(data)

        assert extra_key in str(exc.value)

    def test_a_name_shaped_presenter_id_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(presenter_rights_id="Hanna Musterfrau")

    def test_an_email_shaped_operator_ref_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(
                operator_approval={"approved": True, "approved_by_ref": "hanna@example.com"}
            )

    def test_an_unknown_approval_key_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(operator_approval={"approved": True, "signature_image": "..."})

    def test_the_contract_stays_outside_the_record(self):
        record = _record()

        # Only the pointer, never the document.
        assert record.contract_reference == "CONTRACT-1"
        assert not hasattr(record, "contract_text")


class TestValidation:
    def test_a_valid_record_parses(self):
        record = _record()

        assert record.schema_version == SCHEMA_VERSION
        assert record.presenter_rights_id == "presenter-01"
        assert record.valid_until == date(2030, 1, 1)

    def test_a_wrong_schema_version_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(schema_version=99)

    def test_a_missing_contract_reference_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(contract_reference="")

    def test_a_malformed_date_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(valid_from="01.01.2026")

    def test_an_end_before_the_start_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(valid_from="2026-06-01", valid_until="2026-01-01")

    def test_a_non_boolean_flag_is_refused(self):
        with pytest.raises(RightsRecordError):
            _record(consent_confirmed="yes")

    def test_an_open_ended_release_is_allowed(self):
        record = _record(valid_until=None)

        assert record.valid_until is None
        assert rights_problems(
            record, channel="almanya24-youtube", territory="DE", today=date(2099, 1, 1)
        ) == []

    def test_a_missing_file_is_a_rights_error_not_a_crash(self, tmp_path):
        with pytest.raises(RightsRecordError):
            load_rights_record(tmp_path / "nothing.json")

    def test_broken_json_is_a_rights_error(self, tmp_path):
        path = tmp_path / "rights.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(RightsRecordError):
            load_rights_record(path)

    def test_a_record_round_trips_from_disk(self, tmp_path):
        path = tmp_path / "rights.json"
        path.write_text(json.dumps(VALID), encoding="utf-8")

        record = load_rights_record(path)

        assert record.source_path == path


class TestEvaluation:
    def _problems(self, record, **kwargs):
        kwargs.setdefault("channel", "almanya24-youtube")
        kwargs.setdefault("territory", "DE")
        kwargs.setdefault("today", date(2026, 6, 1))
        return rights_problems(record, **kwargs)

    def test_a_complete_release_has_no_problems(self):
        assert self._problems(_record()) == []

    def test_revocation_is_reported_first(self):
        problems = self._problems(_record(revoked=True, revoked_on="2026-05-01"))

        assert problems
        assert "revoked" in problems[0].lower()

    @pytest.mark.parametrize(
        "field",
        ["consent_confirmed", "voice_likeness_confirmed", "synthetic_video_confirmed"],
    )
    def test_every_confirmation_is_required(self, field):
        problems = self._problems(_record(**{field: False}))

        assert any(field in problem for problem in problems)

    def test_an_expired_release_is_reported(self):
        problems = self._problems(_record(valid_until="2026-02-01"))

        assert any("expired" in problem for problem in problems)

    def test_a_release_before_its_start_is_reported(self):
        problems = self._problems(_record(valid_from="2027-01-01", valid_until="2030-01-01"))

        assert any("not valid before" in problem for problem in problems)

    def test_an_uncovered_channel_is_reported(self):
        problems = self._problems(_record(), channel="other-channel")

        assert any("Channel" in problem for problem in problems)

    def test_an_uncovered_territory_is_reported(self):
        problems = self._problems(_record(), territory="US")

        assert any("Territory" in problem for problem in problems)

    def test_worldwide_covers_everything(self):
        record = _record(permitted_territories=["worldwide"])

        assert self._problems(record, territory="JP") == []

    def test_channel_matching_ignores_case(self):
        assert self._problems(_record(), channel="ALMANYA24-YouTube") == []

    def test_a_missing_operator_approval_is_reported(self):
        problems = self._problems(_record(operator_approval={"approved": False}))

        assert any("operator_approval" in problem for problem in problems)

    def test_an_approval_without_a_reference_is_reported(self):
        problems = self._problems(_record(operator_approval={"approved": True}))

        assert any("approved_by_ref" in problem for problem in problems)

    def test_a_missing_disclosure_is_reported(self):
        problems = self._problems(_record(ai_disclosure_text=""))

        assert any("ai_disclosure_text" in problem for problem in problems)

    def test_all_problems_are_reported_at_once(self):
        record = _record(
            consent_confirmed=False,
            voice_likeness_confirmed=False,
            ai_disclosure_text="",
        )

        problems = self._problems(record)

        # An operator chasing a release wants the whole list, not to rediscover
        # the next missing signature on every run.
        assert len(problems) >= 3


class TestTheShippedExample:
    def test_the_example_record_is_valid_and_fictional(self):
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent
            / "assets"
            / "almanya24"
            / "rights"
            / "anchor-rights.example.json"
        )
        record = load_rights_record(path)

        assert record.presenter_rights_id == "almanya24-presenter-01"
        assert record.contract_reference

    def test_the_real_record_is_not_committed(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        assert not (repo / "assets" / "almanya24" / "rights" / "anchor-rights.json").exists()
        ignored = (repo / ".gitignore").read_text(encoding="utf-8")
        assert "anchor-rights.json" in ignored
