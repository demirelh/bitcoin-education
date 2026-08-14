"""Tests for JobManager._do_imagegen provider dispatch.

Regression guard: the web single-stage imagegen job must dispatch on the
profile's imagegen.provider (like pipeline._run_stage), NOT hardcode Gemini
for tagesschau_tr.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from btcedu.web.jobs import Job, JobManager


@pytest.fixture
def manager(tmp_path):
    return JobManager(str(tmp_path / "logs"))


@pytest.fixture
def job():
    return Job(job_id="j1", episode_id="ep1", action="imagegen", force=True)


def _session_returning(episode):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = episode
    return session


def test_imagegen_generative_calls_generate_images(manager, job):
    ep = SimpleNamespace(episode_id="ep1", content_profile="tagesschau_tr")
    session = _session_returning(ep)
    settings = SimpleNamespace(gemini_image_edit_enabled=True, gemini_api_key="k")

    gen_result = SimpleNamespace(generated_count=5, template_count=0, failed_count=0, cost_usd=0.12)

    with (
        patch("btcedu.core.pipeline._imagegen_provider", return_value="generative"),
        patch("btcedu.core.image_generator.generate_images", return_value=gen_result) as mock_gen,
        patch("btcedu.core.frame_editor.edit_frames") as mock_edit,
    ):
        manager._do_imagegen(job, session, settings)

    mock_gen.assert_called_once()
    mock_edit.assert_not_called()


def test_imagegen_gemini_profile_calls_edit_frames(manager, job):
    ep = SimpleNamespace(episode_id="ep1", content_profile="tagesschau_tr")
    session = _session_returning(ep)
    settings = SimpleNamespace(gemini_image_edit_enabled=True, gemini_api_key="k")

    edit_result = SimpleNamespace(chapters_edited=3, chapters_skipped=0, total_cost_usd=0.03)

    with (
        patch("btcedu.core.pipeline._imagegen_provider", return_value="gemini_frame_edit"),
        patch("btcedu.core.frame_editor.edit_frames", return_value=edit_result) as mock_edit,
        patch("btcedu.core.image_generator.generate_images") as mock_gen,
    ):
        manager._do_imagegen(job, session, settings)

    mock_edit.assert_called_once()
    mock_gen.assert_not_called()
