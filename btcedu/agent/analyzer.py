"""Project analyzer — collects code quality signals without LLM calls."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCAN_MARKERS = ("TODO", "FIXME", "HACK", "XXX")
MAX_FILE_LINES = 500


@dataclass
class AnalysisResult:
    """Aggregated project analysis data."""

    ruff_errors: list[str] = field(default_factory=list)
    test_summary: str = ""
    test_passed: bool = True
    todo_items: list[dict[str, str]] = field(default_factory=list)
    large_files: list[dict[str, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def has_findings(self) -> bool:
        return bool(self.ruff_errors or not self.test_passed or self.todo_items or self.large_files)

    def to_summary(self) -> str:
        parts = []
        if self.ruff_errors:
            parts.append(f"## Lint Errors ({len(self.ruff_errors)})")
            for e in self.ruff_errors[:20]:
                parts.append(f"- {e}")
            if len(self.ruff_errors) > 20:
                parts.append(f"  ... and {len(self.ruff_errors) - 20} more")
        if self.test_summary:
            parts.append(f"## Test Results\n{self.test_summary}")
        if self.todo_items:
            parts.append(f"## TODOs/FIXMEs ({len(self.todo_items)})")
            for t in self.todo_items[:15]:
                parts.append(f"- `{t['file']}:{t['line']}` {t['marker']}: {t['text']}")
        if self.large_files:
            parts.append(f"## Large Files (>{MAX_FILE_LINES} lines)")
            for f in self.large_files:
                parts.append(f"- `{f['file']}`: {f['lines']} lines")
        if not parts:
            parts.append("No issues found. Project looks clean!")
        return "\n".join(parts)


def run_analysis(project_root: Path) -> AnalysisResult:
    """Run all analyzers and return aggregated results."""
    result = AnalysisResult()

    try:
        result.ruff_errors = _run_ruff(project_root)
    except Exception as e:
        logger.warning("ruff analysis failed: %s", e)

    try:
        result.test_summary, result.test_passed = _run_pytest_summary(project_root)
    except Exception as e:
        logger.warning("pytest analysis failed: %s", e)

    try:
        result.todo_items = _scan_todos(project_root)
    except Exception as e:
        logger.warning("TODO scan failed: %s", e)

    try:
        result.large_files = _find_large_files(project_root)
    except Exception as e:
        logger.warning("File size scan failed: %s", e)

    return result


def _run_ruff(project_root: Path) -> list[str]:
    """Run ruff and return list of error strings."""
    try:
        proc = subprocess.run(
            ["ruff", "check", "btcedu/", "tests/", "--output-format=concise"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        logger.warning("ruff not found in PATH")
        return []
    errors = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return errors


def _run_pytest_summary(project_root: Path) -> tuple[str, bool]:
    """Run pytest in quick mode and return (summary_text, all_passed)."""
    try:
        proc = subprocess.run(
            ["pytest", "tests/", "-q", "--tb=no", "--no-header", "-x"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=600,
            env=_test_env(),
        )
    except FileNotFoundError:
        return "pytest not found", False

    summary = proc.stdout.strip().split("\n")[-1] if proc.stdout.strip() else "no output"
    passed = proc.returncode == 0
    return summary, passed


def _test_env() -> dict[str, str]:
    """Build env dict for pytest (dummy API keys, dry_run)."""
    import os

    env = os.environ.copy()
    env.update(
        {
            "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", "dummy"),
            "OPENAI_API_KEY": env.get("OPENAI_API_KEY", "dummy"),
            "WHISPER_API_KEY": env.get("WHISPER_API_KEY", "dummy"),
            "DRY_RUN": "true",
        }
    )
    return env


def _scan_todos(project_root: Path) -> list[dict[str, str]]:
    """Scan Python files for TODO/FIXME/HACK/XXX markers."""
    results: list[dict[str, str]] = []
    for pattern in ("btcedu/**/*.py", "tests/**/*.py"):
        for filepath in project_root.glob(pattern):
            rel = filepath.relative_to(project_root)
            try:
                for i, line in enumerate(filepath.read_text(encoding="utf-8").splitlines(), 1):
                    for marker in SCAN_MARKERS:
                        if marker in line and not line.strip().startswith("SCAN_MARKERS"):
                            text = line.strip()
                            # Extract just the comment part
                            if "#" in text:
                                text = text[text.index("#") + 1 :].strip()
                            results.append(
                                {
                                    "file": str(rel),
                                    "line": str(i),
                                    "marker": marker,
                                    "text": text[:120],
                                }
                            )
                            break
            except (OSError, UnicodeDecodeError):
                continue
    return results


def _find_large_files(project_root: Path) -> list[dict[str, int]]:
    """Find Python files with more than MAX_FILE_LINES lines."""
    results: list[dict[str, int]] = []
    for filepath in project_root.glob("btcedu/**/*.py"):
        try:
            lines = len(filepath.read_text(encoding="utf-8").splitlines())
            if lines > MAX_FILE_LINES:
                rel = str(filepath.relative_to(project_root))
                results.append({"file": rel, "lines": lines})
        except (OSError, UnicodeDecodeError):
            continue
    results.sort(key=lambda x: x["lines"], reverse=True)
    return results
