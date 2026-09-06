"""Profile-owned avatar configuration: the rules that guard paid calls."""

import pytest

from btcedu.config import Settings
from btcedu.core.anchor_config import (
    PLACEHOLDER_LOOK_ID,
    parse_looks,
    parse_rights,
    parse_studio,
    resolve_anchor_config,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(outputs_dir=str(tmp_path / "outputs"))


class TestAlmanya24Profile:
    """The shipped profile is the contract; these pin its material values."""

    def test_almanya24_uses_avatar_iii(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        # Avatar IV is HeyGen's default when `engine` is omitted, so this must
        # be an explicit, asserted choice rather than an inherited one.
        assert config.engine == "avatar_iii"
        assert config.provider == "heygen"
        assert config.avatar_type == "digital_twin"

    def test_price_is_the_documented_avatar_iii_rate(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        # 1.00 USD per minute for Avatar III Digital Twin at 720p/1080p.
        assert config.cost_per_second_usd == 0.0167
        assert config.cost_source
        assert config.cost_checked_on

    def test_typical_bulletin_fits_inside_the_stage_budget(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        # A normal bulletin needs 150-200 s of presenter.
        assert round(150 * config.cost_per_second_usd, 2) == 2.50
        assert round(200 * config.cost_per_second_usd, 2) == 3.34
        assert 200 * config.cost_per_second_usd < config.max_cost_usd
        # ... and the limit still bites well before an implausible episode.
        assert 500 * config.cost_per_second_usd > config.max_cost_usd

    def test_compositing_asks_for_alpha_and_a_fixed_frame(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert config.studio_mode == "composite"
        assert config.output_format == "webm"
        # 'auto' would return the source's ratio, but the presenter is
        # composited into a studio of known geometry.
        assert config.aspect_ratio == "16:9"

    def test_concurrency_stays_below_the_provider_ceiling(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert 1 <= config.max_concurrent_jobs < 10

    def test_ten_placeholder_looks_ship_inactive(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert len(config.looks) == 10
        # Phase 1 has not happened yet: nothing may be usable.
        assert config.active_looks == ()
        assert all(look.is_placeholder for look in config.looks)

    def test_rights_start_unproven_and_disclosure_required(self, settings):
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert config.rights.consent_documented is False
        assert config.rights.revoked is False
        assert config.rights.ai_disclosure_required is True


class TestExistingProfilesAreUntouched:
    def test_bitcoin_podcast_still_resolves_to_d_id(self, settings):
        config = resolve_anchor_config("bitcoin_podcast", settings)

        assert config.provider == "d-id"
        assert config.engine == "talks"
        assert config.output_format == "mp4"
        # The avatar pool is a HeyGen concept and must not leak into D-ID.
        assert config.looks == ()
        assert config.studio_mode == "baked"


class TestCostGuardRules:
    """A rate that cannot stop spending is worse than a wrong one."""

    def test_zero_rate_disables_the_budget_and_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "cost_per_second_usd": 0.0},
        )
        with pytest.raises(ValueError, match="greater than zero"):
            resolve_anchor_config("tagesschau_tr", settings)

    def test_negative_rate_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "cost_per_second_usd": -1},
        )
        with pytest.raises(ValueError, match="greater than zero"):
            resolve_anchor_config("tagesschau_tr", settings)

    def test_missing_rate_falls_back_to_the_published_table(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "engine": "avatar_iii"},
        )
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert config.cost_per_second_usd == 0.0167


class TestStudioModeConsistency:
    def test_composite_without_alpha_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {
                "provider": "heygen",
                "engine": "avatar_iii",
                "studio_mode": "composite",
                "output_format": "mp4",
            },
        )
        # There is no way to key a presenter out of an opaque MP4, so this
        # combination would silently produce a full-frame avatar.
        with pytest.raises(ValueError, match="requires output_format 'webm'"):
            resolve_anchor_config("tagesschau_tr", settings)

    def test_baked_mode_accepts_opaque_mp4(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {
                "provider": "heygen",
                "engine": "avatar_iii",
                "studio_mode": "baked",
                "output_format": "mp4",
            },
        )
        config = resolve_anchor_config("tagesschau_tr", settings)

        assert config.studio_mode == "baked"
        assert config.output_format == "mp4"

    def test_unknown_studio_mode_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "studio_mode": "greenscreen"},
        )
        with pytest.raises(ValueError, match="studio_mode"):
            resolve_anchor_config("tagesschau_tr", settings)


class TestConcurrencyBounds:
    def test_above_provider_ceiling_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "max_concurrent_jobs": 11},
        )
        with pytest.raises(ValueError, match="concurrent jobs"):
            resolve_anchor_config("tagesschau_tr", settings)

    def test_zero_concurrency_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_config.load_profile_anchor_config",
            lambda *_a, **_k: {"provider": "heygen", "max_concurrent_jobs": 0},
        )
        with pytest.raises(ValueError, match="at least 1"):
            resolve_anchor_config("tagesschau_tr", settings)


class TestLookPool:
    def test_active_placeholder_is_refused(self):
        # Left active, this string would be posted to HeyGen as a literal
        # avatar ID and rejected only after the request was made.
        with pytest.raises(ValueError, match="placeholder"):
            parse_looks(
                [
                    {"name": "look_01", "avatar_look_id": PLACEHOLDER_LOOK_ID, "active": True},
                ]
            )

    def test_inactive_placeholder_is_allowed(self):
        looks = parse_looks(
            [{"name": "look_01", "avatar_look_id": PLACEHOLDER_LOOK_ID, "active": False}]
        )

        assert len(looks) == 1
        assert looks[0].is_placeholder

    def test_duplicate_active_ids_are_refused(self):
        # Two entries pointing at one look would collapse the rotation into a
        # single repeated outfit without any visible error.
        with pytest.raises(ValueError, match="unique avatar_look_id"):
            parse_looks(
                [
                    {"name": "look_01", "avatar_look_id": "same", "active": True},
                    {"name": "look_02", "avatar_look_id": "same", "active": True},
                ]
            )

    def test_duplicate_inactive_ids_are_tolerated(self):
        looks = parse_looks(
            [
                {"name": "look_01", "avatar_look_id": "same", "active": False},
                {"name": "look_02", "avatar_look_id": "same", "active": False},
            ]
        )

        assert len(looks) == 2

    def test_duplicate_names_are_refused(self):
        with pytest.raises(ValueError, match="Duplicate anchor look name"):
            parse_looks(
                [
                    {"name": "look_01", "avatar_look_id": "a", "active": False},
                    {"name": "look_01", "avatar_look_id": "b", "active": False},
                ]
            )

    def test_missing_id_is_refused(self):
        with pytest.raises(ValueError, match="missing avatar_look_id"):
            parse_looks([{"name": "look_01", "active": False}])

    def test_missing_name_is_refused(self):
        with pytest.raises(ValueError, match="missing a name"):
            parse_looks([{"avatar_look_id": "a"}])

    def test_active_looks_keep_profile_order(self):
        looks = parse_looks(
            [
                {"name": "look_01", "avatar_look_id": "a", "active": True},
                {"name": "look_02", "avatar_look_id": "b", "active": False},
                {"name": "look_03", "avatar_look_id": "c", "active": True},
            ]
        )
        active = tuple(look.name for look in looks if look.active)

        # Rotation breaks ties on this order, so it must be stable.
        assert active == ("look_01", "look_03")


class TestStudioAndRights:
    def test_studio_defaults_are_conservative(self):
        studio = parse_studio(None)

        assert studio.asset_dir == ""
        assert studio.required_version == 1

    def test_studio_version_must_be_positive(self):
        with pytest.raises(ValueError, match="required_version"):
            parse_studio({"required_version": 0})

    def test_disclosure_defaults_to_required(self):
        rights = parse_rights(None)

        # A missing rights block must not read as "no disclosure needed".
        assert rights.ai_disclosure_required is True
        assert rights.consent_documented is False
        assert rights.revoked is False

    def test_revocation_is_read_verbatim(self):
        rights = parse_rights({"revoked": True, "consent_reference": "contract-2026-01"})

        assert rights.revoked is True
        assert rights.consent_reference == "contract-2026-01"

    def test_channels_and_territories_normalise_to_tuples(self):
        rights = parse_rights(
            {"permitted_channels": ["youtube", " almanya24 "], "permitted_territories": "DE"}
        )

        assert rights.permitted_channels == ("youtube", "almanya24")
        assert rights.permitted_territories == ("DE",)
