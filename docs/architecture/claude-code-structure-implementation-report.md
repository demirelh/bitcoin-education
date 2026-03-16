# Claude Code Structure — Implementation Report

**Date:** 2026-03-16
**Implementor:** Claude Opus 4.6
**Plan:** `docs/architecture/claude-code-structure-plan.md`

---

## 1. What Was Created

### New files: 31 total

| Category | Count | Files |
|----------|-------|-------|
| Root CLAUDE.md | 1 | `CLAUDE.md` (rewritten from 459 -> ~90 lines) |
| Scoped CLAUDE.md | 6 | `btcedu/core/`, `btcedu/models/`, `btcedu/services/`, `btcedu/web/`, `btcedu/prompts/`, `tests/` |
| Project settings | 1 | `.claude/settings.json` |
| Agents | 3 | `pipeline-debug`, `review-workflow`, `visual-assets` |
| Skills | 6 | `deploy`, `test`, `smoke-test-video`, `pipeline-status`, `investigate-failure`, `validate-phase` |
| Architecture docs | 5 | `pipeline-overview`, `review-gate-flow`, `visual-asset-flow`, `render-flow`, `claude-code-structure-plan` |
| Decision records | 4 | `001-stock-images-over-ai`, `002-video-clips-opt-in`, `003-granular-review-model`, `004-sidecar-not-overwrite` |
| Runbooks | 5 | `run-pipeline`, `handle-review-gates`, `debug-failed-episode`, `smoke-test-video`, `safe-recovery` |
| This report | 1 | `claude-code-structure-implementation-report.md` |

### Directory structure created

```
.claude/
├── settings.json                          # shared project settings (committed)
├── settings.local.json                    # cleaned up (gitignored)
├── agents/
│   ├── pipeline-debug/pipeline-debug.md
│   ├── review-workflow/review-workflow.md
│   └── visual-assets/visual-assets.md
└── skills/
    ├── deploy/SKILL.md
    ├── test/SKILL.md
    ├── smoke-test-video/SKILL.md
    ├── pipeline-status/SKILL.md
    ├── investigate-failure/SKILL.md
    └── validate-phase/SKILL.md

docs/
├── architecture/
│   ├── claude-code-structure-plan.md
│   ├── claude-code-structure-implementation-report.md
│   ├── pipeline-overview.md
│   ├── review-gate-flow.md
│   ├── visual-asset-flow.md
│   └── render-flow.md
├── decisions/
│   ├── 001-stock-images-over-ai.md
│   ├── 002-video-clips-opt-in.md
│   ├── 003-granular-review-model.md
│   └── 004-sidecar-not-overwrite.md
└── runbooks/
    ├── run-pipeline.md
    ├── handle-review-gates.md
    ├── debug-failed-episode.md
    ├── smoke-test-video.md
    └── safe-recovery.md

Scoped CLAUDE.md files:
├── btcedu/core/CLAUDE.md
├── btcedu/models/CLAUDE.md
├── btcedu/services/CLAUDE.md
├── btcedu/web/CLAUDE.md
├── btcedu/prompts/CLAUDE.md
└── tests/CLAUDE.md
```

---

## 2. What Was Updated

| File | Change |
|------|--------|
| `CLAUDE.md` | Rewritten: 459 lines -> ~90 lines. Moved detailed reference material to scoped files. |
| `.claude/settings.local.json` | Cleaned: 62 lines of ad-hoc patterns -> 18 lines of essential local permissions |
| `.gitignore` | Added `.claude/settings.local.json` and `.claude/projects/` to keep machine-specific files out of git |

---

## 3. Decisions from Official Anthropic Docs

| Decision | Source |
|----------|--------|
| CLAUDE.md target <200 lines | Official Claude Code docs: "under 200 lines for better adherence" |
| Scoped CLAUDE.md in subdirectories | Official: "CLAUDE.md files in subdirectories are lazy-loaded when Claude reads files in those directories" |
| `.claude/agents/<name>/<name>.md` with YAML frontmatter | Official sub-agents documentation |
| `.claude/skills/<name>/SKILL.md` with YAML frontmatter | Official skills documentation |
| Skills supersede `.claude/commands/` | Official: commands are legacy, skills are recommended |
| `.claude/settings.json` for project-level (committed) | Official: project settings distinct from local overrides |
| `.claude/settings.local.json` for local-only (gitignored) | Official settings documentation |
| No `.claude/commands/` directory | Official: deprecated in favor of skills |

---

## 4. Repo-Specific Decisions

| Decision | Rationale |
|----------|-----------|
| 3 agents (pipeline-debug, review-workflow, visual-assets) | These are the 3 most complex multi-step workflows that benefit from specialized context |
| 6 skills (deploy, test, smoke-test-video, pipeline-status, investigate-failure, validate-phase) | These are the 6 most frequent operations in daily project work |
| 6 scoped CLAUDE.md files | One per major source subdirectory + tests, where lazy-loading provides targeted context |
| 4 ADRs | The 4 most impactful architectural decisions not obvious from code |
| 5 runbooks | The 5 most common operator tasks |
| No hooks | No clear use case that justifies hook complexity yet |
| No MCP server configs | Not needed for this project's workflow |

---

## 5. Why the New Structure Is Better for Claude Code

| Before | After | Improvement |
|--------|-------|-------------|
| 459-line root CLAUDE.md loaded every session | ~90-line root + lazy-loaded scoped files | 80% less context waste; targeted info when needed |
| No agents | 3 specialized agents | Complex workflows (debug, review, stock assets) get specialized context |
| No skills | 6 reusable skills | Common operations invokable via `/deploy`, `/test`, etc. |
| No project settings | `.claude/settings.json` with common permissions | Shared team settings committed to repo |
| Bloated settings.local.json | Clean 18-line local settings | Reduced noise, separated personal from shared |
| No architecture docs | 4 focused architecture docs | Pipeline, review, stock, render flows documented concisely |
| No decision records | 4 ADRs | Key "why" decisions captured for future context |
| No runbooks | 5 operator runbooks | Common tasks documented step-by-step |

---

## 6. Agents Added

| Agent | Purpose | Tools | Model |
|-------|---------|-------|-------|
| `pipeline-debug` | Diagnose failed pipeline episodes | Read, Glob, Grep, Bash, Agent | sonnet |
| `review-workflow` | Manage review gates (list, inspect, approve/reject) | Read, Glob, Grep, Bash | sonnet |
| `visual-assets` | Stock image/video search, ranking, normalization | Read, Glob, Grep, Bash | sonnet |

---

## 7. Skills Added

| Skill | Invocation | Purpose |
|-------|------------|---------|
| `deploy` | `/deploy` | Run `run.sh`, verify health |
| `test` | `/test [pattern]` | Run pytest with optional filter |
| `smoke-test-video` | `/smoke-test-video` | Verify ffmpeg on Pi |
| `pipeline-status` | `/pipeline-status` | Episode status summary |
| `investigate-failure` | `/investigate-failure <ep_id>` | Diagnose failed episode |
| `validate-phase` | `/validate-phase` | Lint + full test suite |

---

## 8. Scoped CLAUDE.md Files Added

| File | Lazy-loaded when editing | Key content |
|------|------------------------|-------------|
| `btcedu/core/CLAUDE.md` | core/ pipeline stages | Stage implementation pattern (10-step checklist) |
| `btcedu/models/CLAUDE.md` | models/ | MediaAsset separate Base gotcha, enum values |
| `btcedu/services/CLAUDE.md` | services/ | Protocol pattern, raw HTTP vs SDK |
| `btcedu/web/CLAUDE.md` | web/ | Flask app factory, API endpoint conventions |
| `btcedu/prompts/CLAUDE.md` | prompts/ | Template format, frontmatter, Jinja2 patterns |
| `tests/CLAUDE.md` | tests/ | Fixture patterns, 5 critical test gotchas |

---

## 9. Runbooks / Architecture / Decision Docs Added

### Architecture (docs/architecture/)
- `pipeline-overview.md` — v1/v2 stages, orchestration, guards, automation
- `review-gate-flow.md` — gate lifecycle, auto-approve, granular review
- `visual-asset-flow.md` — search, rank, finalize, asset types, normalization
- `render-flow.md` — ffmpeg operations, Pi considerations, error handling

### Decision Records (docs/decisions/)
- `001-stock-images-over-ai.md` — why Pexels replaced DALL-E
- `002-video-clips-opt-in.md` — why video is optional
- `003-granular-review-model.md` — why per-item review uses normalized tables
- `004-sidecar-not-overwrite.md` — why review outputs don't overwrite originals

### Runbooks (docs/runbooks/)
- `run-pipeline.md` — automated + manual pipeline operation
- `handle-review-gates.md` — finding, inspecting, approving/rejecting reviews
- `debug-failed-episode.md` — 6-step systematic debugging
- `smoke-test-video.md` — ffmpeg verification on Pi
- `safe-recovery.md` — status fixes, stale errors, service recovery

---

## 10. Intentionally Deferred Ideas

| Idea | Why deferred |
|------|-------------|
| Hooks (PreToolUse, PostToolUse) | No clear use case yet; would add complexity without proven benefit |
| MCP server configs | Not needed for current workflow |
| `.claude/rules/` directory | Scoped CLAUDE.md achieves the same with official support |
| Agent orchestration chains | Project doesn't need multi-agent coordination yet |
| Memory restructuring | Current `~/.claude/projects/*/memory/` system works; it's machine-local |
| render-pipeline agent | Single stage, not complex enough for dedicated agent |
| docs-maintainer agent | Generic doc editing doesn't need specialization |
