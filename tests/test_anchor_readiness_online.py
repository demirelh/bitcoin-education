"""The optional --online check: read-only, free, and honest about failures.

No test here touches the network. A fake session stands in for HeyGen, and one
test asserts the important negative: the client has no method that could create
or upload anything even if somebody wanted it to.
"""

import json
from pathlib import Path

import pytest
import requests

from btcedu.config import Settings
from btcedu.core.anchor_readiness import (
    STATUS_BLOCKED,
    STATUS_PASS,
    STATUS_WARNING,
    evaluate_readiness,
)
from btcedu.services.heygen_readonly import (
    KIND_AUTH,
    KIND_MISSING,
    KIND_NETWORK,
    KIND_RATE_LIMIT,
    KIND_TIMEOUT,
    HeyGenReadOnlyClient,
    HeyGenReadOnlyError,
)


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    """Records every call, and refuses to be used for anything but GET."""

    def __init__(self, responses):
        self.headers = {}
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        del timeout
        self.calls.append(url)
        result = self._responses.get(url)
        if result is None:
            for pattern, value in self._responses.items():
                if url.startswith(pattern):
                    result = value
                    break
        if result is None:
            return _Response(404, {"error": {"message": "not found"}})
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a read-only check attempted a POST")


def _client(responses) -> tuple[HeyGenReadOnlyClient, _FakeSession]:
    session = _FakeSession(responses)
    return HeyGenReadOnlyClient("hg-secret-key", session=session), session


class TestTheClientIsReadOnly:
    def test_it_cannot_create_or_upload(self):
        client, _ = _client({})

        for forbidden in ("submit_anchor_video", "generate_anchor_video", "upload", "create"):
            assert not hasattr(client, forbidden)

    def test_it_only_ever_issues_gets(self):
        client, session = _client(
            {"https://api.heygen.com/v2/avatars": _Response(200, {"data": {"avatars": []}})}
        )

        client.authenticate()

        assert session.calls == ["https://api.heygen.com/v2/avatars"]

    def test_the_api_key_travels_in_a_header_not_a_url(self):
        client, session = _client(
            {"https://api.heygen.com/v2/avatars": _Response(200, {"data": {}})}
        )

        client.authenticate()

        assert session.headers["x-api-key"] == "hg-secret-key"
        assert all("hg-secret-key" not in url for url in session.calls)


class TestErrorsAreDistinguished:
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_key_is_an_auth_fault(self, status):
        client, _ = _client({"https://api.heygen.com/v2/avatars": _Response(status, {})})

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert exc.value.kind == KIND_AUTH

    def test_a_timeout_is_not_an_auth_fault(self):
        client, _ = _client({"https://api.heygen.com/v2/avatars": requests.Timeout("slow")})

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert exc.value.kind == KIND_TIMEOUT

    def test_a_network_error_is_its_own_kind(self):
        client, _ = _client(
            {"https://api.heygen.com/v2/avatars": requests.ConnectionError("no route")}
        )

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert exc.value.kind == KIND_NETWORK

    def test_a_rate_limit_reports_the_provider_s_retry_after(self):
        client, _ = _client(
            {
                "https://api.heygen.com/v2/avatars": _Response(
                    429, {}, headers={"Retry-After": "42"}
                )
            }
        )

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert exc.value.kind == KIND_RATE_LIMIT
        assert exc.value.retry_after == 42.0

    def test_a_rate_limit_without_a_header_still_reports_the_kind(self):
        client, _ = _client({"https://api.heygen.com/v2/avatars": _Response(429, {})})

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert exc.value.kind == KIND_RATE_LIMIT
        assert exc.value.retry_after is None

    def test_an_unknown_look_is_missing_not_broken(self):
        client, _ = _client({"https://api.heygen.com/v2/avatars/nope": _Response(404, {})})

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.look("nope")

        assert exc.value.kind == KIND_MISSING

    def test_no_error_message_quotes_the_key(self):
        client, _ = _client({"https://api.heygen.com/v2/avatars": _Response(401, {})})

        with pytest.raises(HeyGenReadOnlyError) as exc:
            client.authenticate()

        assert "hg-secret-key" not in str(exc.value)


class TestLookLookup:
    def test_a_reported_engine_list_is_parsed(self):
        client, _ = _client(
            {
                "https://api.heygen.com/v2/avatars/look-1": _Response(
                    200,
                    {
                        "data": {
                            "avatar_id": "avatar-9",
                            "avatar_name": "Presenter",
                            "supported_api_engines": ["Avatar_III", "avatar_iv"],
                        }
                    },
                )
            }
        )

        look = client.look("look-1")

        assert look.avatar_id == "avatar-9"
        assert look.supported_api_engines == frozenset({"avatar_iii", "avatar_iv"})

    def test_an_absent_engine_list_stays_unknown(self):
        client, _ = _client(
            {"https://api.heygen.com/v2/avatars/look-1": _Response(200, {"data": {}})}
        )

        look = client.look("look-1")

        # Unknown, not empty. Turning silence into "does not support Avatar III"
        # would block a working look on a field the provider never promised.
        assert look.supported_api_engines is None


def _studio(tmp_path: Path) -> Path:
    studio_dir = tmp_path / "studio"
    studio_dir.mkdir(parents=True, exist_ok=True)
    for name in ("background.png", "fallback.png"):
        (studio_dir / name).write_bytes(b"\x00" * 64)
    (studio_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "studio_version": "1.0.0",
                "asset_version": "1.0.0",
                "name": "Studio",
                "width": 1920,
                "height": 1080,
                "fps": 25,
                "alpha_mode": "alpha_webm",
                "background": {"path": "background.png", "kind": "image"},
                "fallback_display_media": {"path": "fallback.png", "kind": "image"},
                "display_zone": {
                    "zone_id": "monitor",
                    "rect": {"x": 900, "y": 200, "width": 800, "height": 450},
                    "presenter_free": True,
                },
                "presenter": {"anchor_x": 600, "anchor_y": 1080, "scale": 1.0},
                "logo_zone": {"x": 40, "y": 40, "width": 200, "height": 80},
                "safe_areas": {
                    "lower_third": {"x": 100, "y": 820, "width": 1000, "height": 140},
                    "ticker": {"x": 0, "y": 980, "width": 1920, "height": 100},
                    "subtitle": {"x": 200, "y": 900, "width": 1520, "height": 80},
                },
            }
        ),
        encoding="utf-8",
    )
    return studio_dir


class _FakeProfile:
    def __init__(self, anchor):
        self.stage_config = {"anchor": anchor}


@pytest.fixture
def online_world(tmp_path, monkeypatch):
    studio_dir = _studio(tmp_path)
    anchor = {
        "provider": "heygen",
        "engine": "avatar_iii",
        "avatar_type": "digital_twin",
        "output_format": "webm",
        "studio_mode": "composite",
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "cost_per_second_usd": 0.0167,
        "max_cost_usd": 7.0,
        "avatar_id": "avatar-9",
        "looks": [{"name": "look_01", "avatar_look_id": "look-1", "active": True}],
        "studio": {"asset_dir": str(studio_dir), "manifest": "manifest.json"},
        "rights": {},
    }

    class _Registry:
        def get(self, name):
            del name
            return _FakeProfile(anchor)

    monkeypatch.setattr("btcedu.profiles.get_registry", lambda s: _Registry())
    monkeypatch.setattr(
        "btcedu.core.anchor_readiness._profile_dict", lambda profile, settings: {}
    )
    settings = Settings(
        outputs_dir=str(tmp_path / "outputs"),
        render_resolution="1920x1080",
        render_fps=25,
        anchor_enabled=True,
        heygen_api_key="hg-secret-key",
        max_episode_cost_usd=15.0,
    )
    return {"settings": settings, "anchor": anchor}


def _install_client(monkeypatch, responses):
    session = _FakeSession(responses)

    def factory(api_key, **kwargs):
        del kwargs
        return HeyGenReadOnlyClient(api_key, session=session)

    monkeypatch.setattr("btcedu.services.heygen_readonly.HeyGenReadOnlyClient", factory)
    return session


def _status(report, check_id):
    for result in report.results:
        if result.check_id == check_id:
            return result.status
    raise AssertionError(f"No check {check_id!r}: {[r.check_id for r in report.results]}")


class TestOnlineReadiness:
    def test_a_good_look_passes(self, online_world, monkeypatch):
        _install_client(
            monkeypatch,
            {
                "https://api.heygen.com/v2/avatars/look-1": _Response(
                    200,
                    {
                        "data": {
                            "avatar_id": "avatar-9",
                            "supported_api_engines": ["avatar_iii"],
                        }
                    },
                ),
                "https://api.heygen.com/v2/avatars": _Response(200, {"data": {}}),
            },
        )

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert _status(report, "provider.api_key") == STATUS_PASS
        assert _status(report, "provider.look.look_01") == STATUS_PASS
        assert _status(report, "provider.look_engine.look_01") == STATUS_PASS

    def test_a_look_that_lacks_avatar_iii_blocks(self, online_world, monkeypatch):
        _install_client(
            monkeypatch,
            {
                "https://api.heygen.com/v2/avatars/look-1": _Response(
                    200,
                    {"data": {"avatar_id": "avatar-9", "supported_api_engines": ["avatar_iv"]}},
                ),
                "https://api.heygen.com/v2/avatars": _Response(200, {"data": {}}),
            },
        )

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert _status(report, "provider.look_engine.look_01") == STATUS_BLOCKED

    def test_a_look_belonging_to_another_avatar_blocks(self, online_world, monkeypatch):
        _install_client(
            monkeypatch,
            {
                "https://api.heygen.com/v2/avatars/look-1": _Response(
                    200, {"data": {"avatar_id": "someone-else"}}
                ),
                "https://api.heygen.com/v2/avatars": _Response(200, {"data": {}}),
            },
        )

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert _status(report, "provider.look_owner.look_01") == STATUS_BLOCKED

    def test_a_rejected_key_blocks_and_skips_the_look_checks(self, online_world, monkeypatch):
        _install_client(monkeypatch, {"https://api.heygen.com/v2/avatars": _Response(401, {})})

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert _status(report, "provider.api_key") == STATUS_BLOCKED
        assert not any(r.check_id.startswith("provider.look") for r in report.results)

    def test_a_network_failure_only_warns(self, online_world, monkeypatch):
        _install_client(
            monkeypatch,
            {"https://api.heygen.com/v2/avatars": requests.ConnectionError("no route")},
        )

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        # An unreachable provider says nothing about the configuration, and the
        # offline verdict must not be poisoned by a flaky minute of network.
        assert _status(report, "provider.api_key") == STATUS_WARNING

    def test_a_missing_api_key_blocks_before_any_request(self, online_world, monkeypatch):
        online_world["settings"].heygen_api_key = ""
        session = _install_client(monkeypatch, {})

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert _status(report, "provider.api_key") == STATUS_BLOCKED
        assert session.calls == []

    def test_no_provider_check_runs_without_the_flag(self, online_world, monkeypatch):
        session = _install_client(monkeypatch, {})

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=False, studio_mode="alpha_webm"
        )

        assert session.calls == []
        assert not any(r.area == "provider" for r in report.results)

    def test_the_online_report_never_echoes_the_key(self, online_world, monkeypatch):
        _install_client(monkeypatch, {"https://api.heygen.com/v2/avatars": _Response(401, {})})

        report = evaluate_readiness(
            "almanya24", online_world["settings"], online=True, studio_mode="alpha_webm"
        )

        assert "hg-secret-key" not in json.dumps(report.to_dict())
