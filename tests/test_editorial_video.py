"""Videos derived from an approved article (N7).

The tests below ask one question repeatedly: can the video say something the
article was never allowed to say? Every route to that outcome — invented text,
a weakened claim, a revoked picture, a missing archive notice, an approval that
outlives the thing it approved — has a test that closes it. Two more check that
nothing here uploads anything and that a render package cannot carry the
newsroom's private research with it.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from btcedu.core.editorial import video as video_module
from btcedu.core.editorial.article import (
    approve_article_revision,
    article_preview,
    generate_article_revision,
)
from btcedu.core.editorial.media import revoke_media_decision
from btcedu.core.editorial.video import (
    EditionBlocked,
    EditionError,
    PrivateMaterialInPackage,
    StaleEditionApproval,
    approve_final_video,
    approve_script,
    approve_video_media,
    assert_no_private_material,
    broadcast_script,
    build_edition,
    collect_render_inputs,
    edition_blockers,
    edition_media,
    edition_metadata,
    edition_routing_enabled,
    edition_segments,
    plan_segments,
    refresh_edition_status,
    reject_script,
    script_hash,
    segment_claims,
)
from btcedu.models.article import ArticleParagraph, ArticleStatus
from btcedu.models.editorial import ClaimAssessment
from btcedu.models.media_rights import MediaUseDecision
from btcedu.models.script_schema import SegmentPurpose, SpeakerRole
from btcedu.models.video_edition import (
    EditionDecision,
    EditionDecisionKind,
    EditionStatus,
    VideoEdition,
)
from btcedu.profiles import ContentProfile
from tests.test_editorial_article import _draft, _pipeline  # noqa: F401


def _approved(db_session, tmp_path, *, role=None, **kwargs):
    revision, run, _ = _pipeline(db_session, tmp_path, **kwargs)
    if role:
        db_session.query(MediaUseDecision).one().role = role
        db_session.commit()
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)
    approve_article_revision(
        db_session,
        article,
        research_run=run,
        operator_ref="web:editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )
    return article, revision, run


def _weaken_evidence(db_session, run):
    """Make the evidence behind the article worse than it was at approval."""
    assessment = db_session.query(ClaimAssessment).filter_by(research_run_id=run.id).first()
    assessment.verdict = "insufficient"
    db_session.commit()


def _profile(**stage_config) -> ContentProfile:
    return ContentProfile(
        name="almanya24_tr",
        display_name="ALMANYA24",
        source_language="de",
        target_language="tr",
        domain="news",
        stage_config=stage_config,
    )


# ---------------------------------------------------------------------------
# Routing stays off unless a profile asks for it
# ---------------------------------------------------------------------------


def test_an_existing_profile_keeps_its_old_video_route(db_session):
    """No profile gains a new production path by upgrading."""
    assert edition_routing_enabled(_profile()) is False
    assert edition_routing_enabled(None) is False
    assert edition_routing_enabled(_profile(editorial_video={"enabled": False})) is False


def test_a_profile_can_opt_into_the_editorial_route(db_session):
    assert edition_routing_enabled(_profile(editorial_video={"enabled": True})) is True


# ---------------------------------------------------------------------------
# The script is the article, rearranged
# ---------------------------------------------------------------------------


def test_the_edition_speaks_only_sentences_the_article_already_carried(db_session, tmp_path):
    """Dramaturgy may change; substance may not."""
    article, _, run = _approved(db_session, tmp_path)

    edition = build_edition(db_session, article, research_run=run)
    segments = edition_segments(db_session, edition)

    paragraphs = {
        row.text.strip()
        for row in db_session.query(ArticleParagraph)
        .filter_by(article_revision_id=article.id)
        .all()
    }
    allowed = paragraphs | {article.title.strip(), article.lede.strip()}
    assert [segment.text for segment in segments]
    for segment in segments:
        assert segment.text in allowed


def test_the_opening_and_lede_are_read_by_the_anchor(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)

    edition = build_edition(db_session, article, research_run=run)
    segments = edition_segments(db_session, edition)

    assert segments[0].role == SpeakerRole.ANCHOR.value
    assert segments[0].purpose == SegmentPurpose.OPENING.value
    assert segments[0].text == article.title
    assert segments[1].purpose == SegmentPurpose.INTRODUCTION.value
    assert segments[1].text == article.lede


def test_every_spoken_body_part_keeps_the_claims_of_its_paragraph(db_session, tmp_path):
    """A segment can be traced back to what makes it sayable."""
    article, _, run = _approved(db_session, tmp_path)

    edition = build_edition(db_session, article, research_run=run)
    body = [
        segment
        for segment in edition_segments(db_session, edition)
        if segment.source_paragraph_position is not None
        and segment.purpose == SegmentPurpose.REPORT.value
    ]

    assert body
    for segment in body:
        paragraph = (
            db_session.query(ArticleParagraph)
            .filter_by(
                article_revision_id=article.id,
                position=segment.source_paragraph_position,
            )
            .one()
        )
        assert paragraph.text.strip() == segment.text
        assert segment_claims(db_session, segment)


def test_the_script_uses_the_schema_the_existing_renderer_already_reads(db_session, tmp_path):
    """Reusing BroadcastScript keeps this from becoming a second renderer."""
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)

    script = broadcast_script(db_session, edition)

    assert script.stories[0].display_headline == article.title
    assert script.narration
    assert script.estimated_duration_seconds > 0
    assert script.generated_by == "editorial_adapter"


def test_rebuilding_an_unchanged_edition_returns_the_stored_one(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)

    first = build_edition(db_session, article, research_run=run)
    second = build_edition(db_session, article, research_run=run)

    assert first.id == second.id
    assert db_session.query(VideoEdition).count() == 1


# ---------------------------------------------------------------------------
# A video is never derived from something unfinished
# ---------------------------------------------------------------------------


def test_a_draft_article_cannot_become_a_video(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    with pytest.raises(EditionBlocked):
        build_edition(db_session, article, research_run=run)
    assert db_session.query(VideoEdition).count() == 0


def test_an_article_whose_evidence_weakened_cannot_become_a_video(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    _weaken_evidence(db_session, run)

    with pytest.raises(EditionBlocked):
        build_edition(db_session, article, research_run=run)


# ---------------------------------------------------------------------------
# Media: rights travel with the picture
# ---------------------------------------------------------------------------


def test_an_archive_picture_carries_a_visible_notice(db_session, tmp_path):
    """A picture that is not of the event must say so on screen."""
    article, _, run = _approved(db_session, tmp_path, role="archive")

    edition = build_edition(db_session, article, research_run=run)
    [media] = edition_media(db_session, edition)

    assert media.requires_notice is True
    assert media.on_screen_notice == "ARŞİV"
    assert media.on_screen_credit


def test_event_footage_needs_no_archive_notice(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)

    edition = build_edition(db_session, article, research_run=run)
    [media] = edition_media(db_session, edition)

    assert media.role == "event"
    assert media.requires_notice is False
    assert media.on_screen_notice == ""


def test_attribution_appears_in_the_metadata_meant_for_the_description(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)

    metadata = edition_metadata(db_session, edition)

    assert metadata["title"] == article.title
    assert metadata["credits"]
    assert metadata["article_revision_id"] == article.article_revision_id


def test_a_licence_that_needs_credit_but_has_none_blocks_the_edition(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    decision = db_session.query(MediaUseDecision).one()
    decision.attribution_text = "   "
    db_session.commit()

    with pytest.raises(EditionBlocked):
        build_edition(db_session, article, research_run=run)


# ---------------------------------------------------------------------------
# Approvals are revocable by evidence
# ---------------------------------------------------------------------------


def test_a_script_approval_names_the_person_and_the_state(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)

    decision = approve_script(
        db_session, edition, operator_ref="cli:nadine", research_run=run
    )

    assert decision.kind == EditionDecisionKind.SCRIPT.value
    assert decision.operator_ref == "cli:nadine"
    assert decision.script_hash == edition.script_hash
    assert edition.status == EditionStatus.SCRIPT_APPROVED.value


def test_an_unnamed_operator_cannot_approve_a_script(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)

    with pytest.raises(ValueError):
        approve_script(db_session, edition, operator_ref="  ", research_run=run)
    assert edition.status == EditionStatus.DRAFT.value


def test_a_rejection_has_to_say_why(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)

    with pytest.raises(ValueError):
        reject_script(db_session, edition, operator_ref="cli:nadine", note="")

    reject_script(
        db_session,
        edition,
        operator_ref="cli:nadine",
        note="Zweiter Absatz ueberspitzt die Quelle",
    )
    assert edition.status == EditionStatus.BLOCKED.value
    assert "ueberspitzt" in edition.block_reason


def test_a_weakened_claim_takes_the_script_approval_away(db_session, tmp_path):
    """The approval described a state; the state is gone."""
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    approve_script(db_session, edition, operator_ref="cli:nadine", research_run=run)

    _weaken_evidence(db_session, run)
    refresh_edition_status(db_session, edition, research_run=run)

    assert edition.status == EditionStatus.BLOCKED.value
    assert "evidence" in edition.block_reason.lower()


def test_a_revoked_picture_blocks_the_edition(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    decision = db_session.query(MediaUseDecision).one()

    revoke_media_decision(
        db_session,
        decision,
        reason="Lizenz zurueckgezogen",
        revoked_by="cli:nadine",
    )
    reasons = edition_blockers(db_session, edition, research_run=run)

    assert reasons
    assert any("media" in reason.lower() for reason in reasons)


def test_a_changed_article_text_invalidates_the_script(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    paragraph = (
        db_session.query(ArticleParagraph)
        .filter_by(article_revision_id=article.id)
        .order_by(ArticleParagraph.position.desc())
        .first()
    )
    paragraph.text = "Berlin 900 yeni konut bildirdi."
    db_session.commit()

    reasons = edition_blockers(db_session, edition, research_run=run)

    assert any("script" in reason.lower() or "text" in reason.lower() for reason in reasons)
    with pytest.raises(StaleEditionApproval):
        approve_script(db_session, edition, operator_ref="cli:nadine", research_run=run)


def test_an_article_withdrawn_from_approval_blocks_the_edition(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    article.status = ArticleStatus.DRAFT.value
    db_session.commit()

    reasons = edition_blockers(db_session, edition, research_run=run)

    assert any("not approved" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Script and finished file are two different decisions
# ---------------------------------------------------------------------------


def test_the_finished_video_needs_its_own_approval(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run, profile=_profile())
    approve_video_media(
        db_session, edition, operator_ref="cli:nadine", research_run=run, note="Video crop"
    )
    rendered = tmp_path / "edition.mp4"
    rendered.write_bytes(b"synthetic")

    with pytest.raises(EditionError):
        approve_final_video(
            db_session,
            edition,
            operator_ref="cli:nadine",
            video_path=rendered,
            research_run=run,
        )

    approve_script(db_session, edition, operator_ref="cli:nadine", research_run=run)
    decision = approve_final_video(
        db_session,
        edition,
        operator_ref="cli:selin",
        video_path=rendered,
        research_run=run,
    )

    assert decision.kind == EditionDecisionKind.FINAL_VIDEO.value
    assert edition.status == EditionStatus.FINAL_APPROVED.value
    assert edition.final_video_path == str(rendered)
    assert db_session.query(EditionDecision).count() == 3


def test_a_final_approval_names_one_specific_file(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    approve_script(db_session, edition, operator_ref="cli:nadine", research_run=run)

    with pytest.raises(EditionError):
        approve_final_video(
            db_session,
            edition,
            operator_ref="cli:selin",
            video_path=tmp_path / "never-rendered.mp4",
            research_run=run,
        )


def test_nothing_in_this_module_uploads_anything(db_session):
    """Publishing stays a separate, manual act.

    Read as code rather than as text: prose may well discuss uploading, but no
    identifier, attribute or import in this module may reach a publisher.
    """
    tree = ast.parse(inspect.getsource(video_module))
    forbidden = ("youtube", "upload", "publish", "googleapis", "publisher")

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)

    offenders = [
        name for name in names if any(word in name.lower() for word in forbidden)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Render packages carry pictures, not the newsroom's files
# ---------------------------------------------------------------------------


def test_a_render_package_contains_only_approved_inputs(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run, profile=_profile())
    approve_script(db_session, edition, operator_ref="cli:nadine", research_run=run)
    approve_video_media(
        db_session, edition, operator_ref="cli:nadine", research_run=run, note="Video crop"
    )
    dest = tmp_path / "package"

    manifest = collect_render_inputs(db_session, edition, dest=dest, research_run=run)

    assert manifest["script_hash"] == edition.script_hash
    assert manifest["media"]
    written = json.loads((dest / "render_inputs.json").read_text(encoding="utf-8"))
    assert written["edition_id"] == edition.edition_id
    names = {path.name for path in dest.rglob("*") if path.is_file()}
    assert "render_inputs.json" in names
    assert not any("evidence" in name for name in names)


def test_private_research_material_can_never_enter_a_package(db_session, tmp_path):
    evidence = tmp_path / "evidence" / "snapshot.html"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("<html></html>", encoding="utf-8")

    with pytest.raises(PrivateMaterialInPackage):
        assert_no_private_material([evidence])


def test_a_stale_edition_cannot_be_packaged(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    edition = build_edition(db_session, article, research_run=run)
    _weaken_evidence(db_session, run)

    with pytest.raises(StaleEditionApproval):
        collect_render_inputs(
            db_session, edition, dest=tmp_path / "package", research_run=run
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_the_script_hash_follows_the_words_and_the_voices(db_session, tmp_path):
    article, _, run = _approved(db_session, tmp_path)
    plans = plan_segments(db_session, article)
    baseline = script_hash(plans)

    reordered = tuple(reversed(plans))

    assert script_hash(plans) == baseline
    assert script_hash(reordered) != baseline


def _run_cli(db_session, args: list[str]):
    from click.testing import CliRunner

    from btcedu.cli import cli

    return CliRunner().invoke(cli, args, obj={"session_factory": lambda: db_session})


def test_the_cli_builds_and_approves_an_edition_under_named_operators(db_session, tmp_path):
    """Two people, two decisions, both recorded."""
    article, _, run = _approved(db_session, tmp_path)

    built = _run_cli(db_session, [
        "edition", "build", article.article_revision_id, "--profile", "almanya24_editorial"
    ])
    assert built.exit_code == 0, built.output
    edition_id = db_session.query(VideoEdition).one().edition_id

    shown = _run_cli(db_session, ["edition", "show", edition_id])
    assert shown.exit_code == 0
    assert "BLOCKED" not in shown.output

    approved = _run_cli(
        db_session,
        ["edition", "approve-script", edition_id, "--operator", "nadine"],
    )
    assert approved.exit_code == 0
    media_approved = _run_cli(db_session, [
        "edition", "approve-media", edition_id, "--operator", "cli:nadine",
        "--note", "Video crop",
    ])
    assert media_approved.exit_code == 0, media_approved.output

    rendered = tmp_path / "edition.mp4"
    rendered.write_bytes(b"synthetic")
    final = _run_cli(
        db_session,
        [
            "edition",
            "approve-video",
            edition_id,
            str(rendered),
            "--operator",
            "selin",
        ],
    )
    assert final.exit_code == 0

    stored = db_session.query(VideoEdition).filter_by(edition_id=edition_id).one()
    assert stored.status == EditionStatus.FINAL_APPROVED.value
    operators = {
        row.operator_ref
        for row in db_session.query(EditionDecision)
        .filter_by(video_edition_id=stored.id)
        .all()
    }
    assert operators == {"cli:nadine", "cli:selin"}


def test_the_cli_refuses_to_build_from_a_draft_article(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    result = _run_cli(db_session, ["edition", "build", article.article_revision_id])

    assert result.exit_code != 0
    assert "not approved" in result.output
    assert db_session.query(VideoEdition).count() == 0
