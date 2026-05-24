"""Tests for btcedu.agent package."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from btcedu.agent.analyzer import AnalysisResult, _find_large_files, _scan_todos, run_analysis
from btcedu.agent.executor import ExecutionResult, execute_suggestions
from btcedu.agent.models import AgentAction, AgentRun
from btcedu.agent.planner import Suggestion, _parse_suggestions, generate_suggestions
from btcedu.agent.runner import run_once
from btcedu.config import Settings

# ---------------------------------------------------------------------------
# Analyzer tests
# ---------------------------------------------------------------------------


class TestAnalysisResult:
    def test_has_findings_empty(self):
        r = AnalysisResult()
        assert not r.has_findings

    def test_has_findings_ruff(self):
        r = AnalysisResult(ruff_errors=["E501 line too long"])
        assert r.has_findings

    def test_has_findings_tests_failed(self):
        r = AnalysisResult(test_passed=False, test_summary="1 failed")
        assert r.has_findings

    def test_has_findings_todos(self):
        todo = {"file": "x.py", "line": "1", "marker": "TODO", "text": "fix"}
        r = AnalysisResult(todo_items=[todo])
        assert r.has_findings

    def test_to_summary_clean(self):
        r = AnalysisResult()
        assert "clean" in r.to_summary().lower()

    def test_to_summary_with_errors(self):
        r = AnalysisResult(ruff_errors=["E501 x.py:1"])
        summary = r.to_summary()
        assert "Lint Errors" in summary
        assert "E501" in summary


class TestScanTodos:
    def test_scan_finds_todo(self, tmp_path):
        f = tmp_path / "btcedu" / "test.py"
        f.parent.mkdir()
        f.write_text("x = 1  # TODO: fix this\n")
        results = _scan_todos(tmp_path)
        assert len(results) == 1
        assert results[0]["marker"] == "TODO"

    def test_scan_skips_non_python(self, tmp_path):
        f = tmp_path / "btcedu" / "readme.md"
        f.parent.mkdir()
        f.write_text("# TODO: fix this\n")
        results = _scan_todos(tmp_path)
        assert len(results) == 0


class TestFindLargeFiles:
    def test_finds_large_file(self, tmp_path):
        f = tmp_path / "btcedu" / "big.py"
        f.parent.mkdir()
        f.write_text("\n".join([f"line_{i} = {i}" for i in range(600)]))
        results = _find_large_files(tmp_path)
        assert len(results) == 1
        assert results[0]["lines"] == 600

    def test_skips_small_file(self, tmp_path):
        f = tmp_path / "btcedu" / "small.py"
        f.parent.mkdir()
        f.write_text("x = 1\n")
        results = _find_large_files(tmp_path)
        assert len(results) == 0


class TestRunAnalysis:
    @patch("btcedu.agent.analyzer._run_ruff", return_value=["E501 x.py:1"])
    @patch("btcedu.agent.analyzer._run_pytest_summary", return_value=("10 passed", True))
    @patch("btcedu.agent.analyzer._scan_todos", return_value=[])
    @patch("btcedu.agent.analyzer._find_large_files", return_value=[])
    def test_aggregates_results(self, mock_large, mock_todo, mock_pytest, mock_ruff):
        result = run_analysis(Path("/fake"))
        assert result.ruff_errors == ["E501 x.py:1"]
        assert result.test_passed is True


# ---------------------------------------------------------------------------
# Planner tests
# ---------------------------------------------------------------------------


class TestParseSuggestions:
    def test_valid_json(self):
        raw = json.dumps([
            {
                "title": "Fix lint", "body": "Fix E501 in x.py",
                "labels": ["bug"], "priority": "high",
            },
            {
                "title": "Refactor", "body": "Split big file",
                "labels": ["enhancement"], "priority": "low",
            },
        ])
        result = _parse_suggestions(raw, max_issues=3)
        assert len(result) == 2
        assert result[0].title == "Fix lint"
        assert result[0].priority == "high"

    def test_invalid_json(self):
        result = _parse_suggestions("not json", max_issues=3)
        assert result == []

    def test_filters_invalid_labels(self):
        raw = json.dumps([
            {"title": "X", "body": "Y", "labels": ["bug", "invalid"], "priority": "high"},
        ])
        result = _parse_suggestions(raw, max_issues=3)
        assert result[0].labels == ["bug"]

    def test_caps_max_issues(self):
        items = [
            {"title": f"T{i}", "body": f"B{i}", "labels": [], "priority": "low"}
            for i in range(10)
        ]
        result = _parse_suggestions(json.dumps(items), max_issues=2)
        assert len(result) == 2

    def test_invalid_priority_defaults_to_medium(self):
        raw = json.dumps([{"title": "X", "body": "Y", "labels": [], "priority": "urgent"}])
        result = _parse_suggestions(raw, max_issues=3)
        assert result[0].priority == "medium"


class TestGenerateSuggestions:
    def test_skips_without_api_key(self):
        settings = Settings(anthropic_api_key="dummy")
        result = generate_suggestions("analysis", settings)
        assert result == []

    def test_calls_api(self):
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"title": "Fix X", "body": "Do Y", "labels": ["bug"], "priority": "high"}
        ]))]
        mock_client.messages.create.return_value = mock_response

        import sys
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            settings = Settings(anthropic_api_key="sk-real-key")
            result = generate_suggestions("analysis data", settings)
        assert len(result) == 1
        assert result[0].title == "Fix X"


# ---------------------------------------------------------------------------
# Executor tests
# ---------------------------------------------------------------------------


class TestExecutor:
    def _make_session(self):
        """Create an in-memory DB session with agent tables."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from btcedu.db import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_dry_run_skips_creation(self):
        session = self._make_session()
        run = AgentRun(status="running")
        session.add(run)
        session.flush()

        suggestions = [Suggestion(title="Fix X", body="Do Y", labels=["bug"], priority="high")]
        settings = Settings(agent_github_repo="test/repo")

        result = execute_suggestions(suggestions, settings, session, run.id, dry_run=True)
        assert result.skipped == 1
        assert result.created == 0

        actions = session.query(AgentAction).all()
        assert len(actions) == 1
        assert actions[0].action_type == "dry_run"

    def test_dedup_by_hash(self):
        session = self._make_session()
        run = AgentRun(status="running")
        session.add(run)
        session.flush()

        suggestion = Suggestion(title="Fix X", body="Do Y", labels=["bug"], priority="high")
        body_hash = hashlib.sha256(("Fix X" + "Do Y").encode()).hexdigest()

        # Pre-populate a "created" action with the same hash
        existing = AgentAction(
            run_id=run.id, action_type="created", title="Fix X",
            body_hash=body_hash, issue_url="https://github.com/test/1",
        )
        session.add(existing)
        session.flush()

        settings = Settings(agent_github_repo="test/repo")
        result = execute_suggestions([suggestion], settings, session, run.id, dry_run=False)
        assert result.skipped == 1

    def test_no_repo_configured(self):
        session = self._make_session()
        run = AgentRun(status="running")
        session.add(run)
        session.flush()

        settings = Settings(agent_github_repo="")
        result = execute_suggestions([], settings, session, run.id)
        assert result.created == 0


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestRunner:
    def _make_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from btcedu.db import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    @patch("btcedu.agent.runner.run_analysis")
    def test_clean_project_no_suggestions(self, mock_analysis):
        mock_analysis.return_value = AnalysisResult()  # no findings
        session = self._make_session()
        settings = Settings(agent_github_repo="test/repo")

        result = run_once(session, settings, project_root=Path("/fake"))
        assert result.status == "success"
        assert result.findings_count == 0

    @patch("btcedu.agent.runner.execute_suggestions")
    @patch("btcedu.agent.runner.generate_suggestions")
    @patch("btcedu.agent.runner.run_analysis")
    def test_full_cycle_dry_run(self, mock_analysis, mock_plan, mock_exec):
        mock_analysis.return_value = AnalysisResult(ruff_errors=["E501 x.py:1"])
        mock_plan.return_value = [
            Suggestion(title="Fix lint", body="Fix E501", labels=["bug"], priority="high")
        ]
        mock_exec.return_value = ExecutionResult(created=0, skipped=1)

        session = self._make_session()
        settings = Settings(agent_github_repo="test/repo", agent_dry_run=True)

        result = run_once(session, settings, project_root=Path("/fake"))
        assert result.status == "success"
        assert result.findings_count == 1
        mock_exec.assert_called_once()

    @patch("btcedu.agent.runner.run_analysis", side_effect=Exception("boom"))
    def test_handles_failure(self, mock_analysis):
        session = self._make_session()
        settings = Settings(agent_github_repo="test/repo")

        result = run_once(session, settings, project_root=Path("/fake"))
        assert result.status == "failed"
        assert "boom" in result.error
