"""Tests for one-shot automatic Copilot repair tracking."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from btcedu.core.copilot_fix import stage_attribution, start_copilot_fix


def _settings(tmp_path):
    return SimpleNamespace(
        outputs_dir=str(tmp_path / "outputs"),
        copilot_cli_binary="copilot",
        copilot_auto_fix_enabled=True,
        copilot_auto_fix_model="gpt-5.6-sol",
        dry_run=False,
    )


@patch("btcedu.core.copilot_fix.subprocess.run")
@patch("btcedu.core.copilot_fix.shutil.which")
def test_same_error_is_started_automatically_only_once(mock_which, mock_run, tmp_path):
    mock_which.side_effect = lambda command: f"/usr/bin/{command}"
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout="", stderr=""),
        MagicMock(returncode=0, stdout="", stderr=""),
        MagicMock(returncode=1, stdout="", stderr=""),
    ]
    settings = _settings(tmp_path)

    first = start_copilot_fix(
        settings,
        "episode-1",
        "Episode One",
        "adapt",
        "Stage 'adapt' failed: invalid operation",
        automatic=True,
        profile="tagesschau_tr",
    )
    second = start_copilot_fix(
        settings,
        "episode-1",
        "Episode One",
        "adapt",
        "Stage 'adapt' failed: invalid operation",
        automatic=True,
        profile="tagesschau_tr",
    )

    assert first.started is True
    assert second.started is False
    assert second.already_attempted is True
    assert mock_run.call_count == 3
    command = mock_run.call_args_list[1].args[0]
    assert command[:6] == [
        "/usr/bin/tmux",
        "new-session",
        "-d",
        "-s",
        "copilotfix",
        "-c",
    ]
    assert "--model gpt-5.6-sol" in command[7]
    assert "--allow-all" in command[7]
    assert "btcedu.core.copilot_fix complete" in command[7]
    assert (
        "btcedu regression-run --from-stage adapt --only-stage "
        "--profile tagesschau_tr --count 3"
    ) in command[7]
    assert "do not commit, do not push, and do not resume" in command[7]

    attribution = stage_attribution(settings.outputs_dir, "episode-1")
    assert attribution["adapt"]["automatic"] is True
    assert attribution["adapt"]["status"] == "running"


@patch("btcedu.core.copilot_fix.subprocess.run")
@patch("btcedu.core.copilot_fix.shutil.which")
def test_manual_fix_requires_three_episode_stage_regression(mock_which, mock_run, tmp_path):
    mock_which.side_effect = lambda command: f"/usr/bin/{command}"
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout="", stderr=""),
        MagicMock(returncode=0, stdout="", stderr=""),
    ]

    result = start_copilot_fix(
        _settings(tmp_path),
        "episode-manual",
        "Manual Episode",
        "render",
        "Stage 'render' failed: ffmpeg error",
        automatic=False,
        profile="tagesschau_tr",
    )

    assert result.started is True
    command = mock_run.call_args_list[1].args[0]
    assert (
        "btcedu regression-run --from-stage render --only-stage "
        "--profile tagesschau_tr --count 3"
    ) in command[7]
    assert "do not commit, do not push, and do not resume" in command[7]
