# Claude Code Structure Plan — btcedu Repository

**Date:** 2026-03-16
**Author:** Claude Opus 4.6 (automated audit + plan)
**Status:** Plan — pending implementation

---

## 1. Current Repository Structure Summary

### What exists today

| Area | State |
|------|-------|
| `CLAUDE.md` (root) | 459 lines — comprehensive but bloated (official recommendation: <200 lines) |
| `.claude/settings.local.json` | Accumulated ad-hoc permission allows (62 lines, mostly one-off bash patterns) |
| `.claude/agents/` | Does not exist |
| `.claude/skills/` | Does not exist |
| `.claude/commands/` | Does not exist |
| `.claude/settings.json` (project) | Does not exist |
| Scoped `CLAUDE.md` files | None |
| `docs/` | Sprint docs (66 files), setup/deploy guides, constraints table, progress log |
| `docs/architecture/` | Does not exist (this plan creates it) |
| `docs/decisions/` | Does not exist |
| `docs/runbooks/` | Does not exist |
| Auto-memory (`MEMORY.md`) | Exists at `~/.claude/projects/*/memory/MEMORY.md` — working |

### Source code partitions (btcedu/)

- `core/` — 18 modules, pipeline orchestration + all v2 stages (~376KB)
- `models/` — 13 files, SQLAlchemy 2.0 models + Pydantic schemas (~68KB)
- `services/` — 11 files, external API wrappers (~112KB)
- `web/` — Flask SPA dashboard, 5 core files (~132KB + static)
- `prompts/` — 12 files, v1 legacy Python + v2 Jinja2 templates (~52KB)
- `migrations/` — 7 migrations in single `__init__.py`
- `utils/` — journal + LLM introspection

### Tests

- 48 test files in `tests/`, ~867 tests passing
- Fixtures in `tests/fixtures/` (3 files)
- `conftest.py` with in-memory SQLite + FTS5

### Deployment

- `deploy/` — systemd units, Caddy config, `setup-web.sh`
- `run.sh` — production deploy script (git pull → pip install → migrate → restart)
- `.github/workflows/` — CI, security scanning, deploy

---

## 2. Problems with Current Claude/AI Context Structure

### P1: Root CLAUDE.md is too large (459 lines)
Official guidance: target <200 lines for better adherence. Current file includes full
enum listings, config tables, all 34 CLI commands, all API endpoints, migration details,
and complete model field listings. Most of this is derivable from code and shouldn't
consume context on every session.

### P2: No scoped CLAUDE.md files
When Claude works on tests, it gets the full 459-line root context but no
test-specific guidance. When working on the web dashboard, no web-specific patterns.
When modifying the pipeline, no pipeline-specific rules. This wastes context and
misses opportunities for targeted instruction.

### P3: No project-level settings.json
All settings are in `settings.local.json` (gitignored). Useful shared settings
(recommended model, common permission patterns) aren't committed.

### P4: No agents for specialized workflows
Common multi-step tasks (debugging failed pipelines, validating phase outputs,
reviewing stock image changes) require Claude to rediscover the workflow each time.

### P5: No skills for recurring operations
Frequent operations (run tests, deploy, smoke-test video, check pipeline status)
aren't encapsulated as reusable skills.

### P6: No architecture / decision / runbook docs
Operational knowledge lives only in MASTERPLAN.md (66KB — too large for context)
or scattered across 66 sprint docs. No concise architecture overview, no ADRs,
no operator runbooks.

### P7: settings.local.json is a mess
62 lines of accumulated one-off permission patterns including full Caddyfile
content embedded in bash patterns. Should be cleaned up and split into
project-level (shared) vs local-only (personal) settings.

---

## 3. Official Anthropic / Claude Code Conventions (verified)

### CLAUDE.md
- **Source:** Official Claude Code documentation
- Root `CLAUDE.md` or `.claude/CLAUDE.md` loaded every session
- Subdirectory `CLAUDE.md` files lazy-loaded when Claude reads files in that directory
- Target: <200 lines for best adherence
- Content: build commands, coding standards, architecture decisions, naming conventions
- Supports `@path/to/file` import syntax for referencing other files

### .claude/agents/
- **Source:** Official Claude Code sub-agents documentation
- Format: `.claude/agents/<name>/<name>.md` with YAML frontmatter
- Frontmatter: name, description, tools, model, permissionMode, maxTurns, etc.
- Body: system prompt and instructions
- Discovery: auto-delegated by description match or explicit invocation

### .claude/skills/
- **Source:** Official Claude Code skills documentation
- Format: `.claude/skills/<name>/SKILL.md` with YAML frontmatter
- Frontmatter: name, description, argument-hint, allowed-tools, model, context, etc.
- Supports `$ARGUMENTS`, `$0`, `$1`, `!`backtick`` shell preprocessing
- Invoked via `/skill-name [args]` or auto-invoked by description match
- Skills supersede legacy `.claude/commands/` (same name → skill wins)

### .claude/commands/
- **Source:** Official Claude Code documentation (legacy)
- Status: **deprecated** — merged into skills system
- **Decision: Will not create.** Skills are the recommended approach.

### .claude/settings.json
- **Source:** Official Claude Code settings documentation
- Project-level (committed): `.claude/settings.json`
- Local overrides (gitignored): `.claude/settings.local.json`
- Supports: permissions, hooks, model, claudeMdExcludes, autoMemoryEnabled

### Hooks
- **Source:** Official Claude Code hooks documentation
- 24 event types (SessionStart, PreToolUse, PostToolUse, Stop, etc.)
- Types: command, http, prompt, agent
- Configured in settings.json or agent/skill frontmatter

### Memory
- **Source:** Official Claude Code memory documentation
- `~/.claude/projects/<project>/memory/MEMORY.md` — first 200 lines always loaded
- Topic files in same directory — read on demand
- Machine-local, not committed to repo

---

## 4. Proposed Target Structure

### New files/directories (additive)

```
.claude/
├── settings.json              # NEW — shared project settings (committed)
├── settings.local.json        # EXISTING — cleaned up (gitignored)
├── agents/
│   ├── pipeline-debug/pipeline-debug.md
│   ├── review-workflow/review-workflow.md
│   └── stock-assets/stock-assets.md
└── skills/
    ├── deploy/SKILL.md
    ├── test/SKILL.md
    ├── smoke-test-video/SKILL.md
    ├── pipeline-status/SKILL.md
    ├── investigate-failure/SKILL.md
    └── validate-phase/SKILL.md

CLAUDE.md                      # REWRITE — trimmed to <200 lines

btcedu/
├── core/CLAUDE.md             # NEW — pipeline stage patterns
├── models/CLAUDE.md           # NEW — model conventions + gotchas
├── services/CLAUDE.md         # NEW — service layer patterns
├── web/CLAUDE.md              # NEW — web dashboard patterns
└── prompts/CLAUDE.md          # NEW — template conventions

tests/CLAUDE.md                # NEW — test patterns + fixtures

docs/
├── architecture/
│   ├── claude-code-structure-plan.md    # THIS FILE
│   ├── pipeline-overview.md            # NEW
│   ├── review-gate-flow.md             # NEW
│   ├── stock-asset-flow.md             # NEW
│   └── render-flow.md                  # NEW
├── decisions/
│   ├── 001-stock-images-over-ai.md     # NEW
│   ├── 002-video-clips-opt-in.md       # NEW
│   ├── 003-granular-review-model.md    # NEW
│   └── 004-sidecar-not-overwrite.md    # NEW
└── runbooks/
    ├── run-pipeline.md                 # NEW
    ├── handle-review-gates.md          # NEW
    ├── debug-failed-episode.md         # NEW
    ├── smoke-test-video.md             # NEW
    └── safe-recovery.md                # NEW
```

### Files to modify
- `CLAUDE.md` — rewrite (459 → ~180 lines)
- `.claude/settings.local.json` — clean up bloated permission patterns
- `.gitignore` — ensure `.claude/settings.local.json` is ignored

### Files NOT to move or delete
- All source code stays exactly where it is
- All test files stay where they are
- All sprint docs preserved as-is
- MASTERPLAN.md preserved as-is (reference document)
- README.md preserved as-is
- deploy/ preserved as-is
- .github/workflows/ preserved as-is

---

## 5. Exact Files to Create, Move, or Update

### Create (31 new files)

| File | Purpose |
|------|---------|
| `.claude/settings.json` | Shared project settings (model, common permissions) |
| `.claude/agents/pipeline-debug/pipeline-debug.md` | Debug failed pipeline episodes |
| `.claude/agents/review-workflow/review-workflow.md` | Manage review gates |
| `.claude/agents/stock-assets/stock-assets.md` | Stock image/video selection |
| `.claude/skills/deploy/SKILL.md` | Run deployment (`run.sh`) |
| `.claude/skills/test/SKILL.md` | Run test suite |
| `.claude/skills/smoke-test-video/SKILL.md` | Run video smoke test |
| `.claude/skills/pipeline-status/SKILL.md` | Show pipeline/episode status |
| `.claude/skills/investigate-failure/SKILL.md` | Investigate failed episode |
| `.claude/skills/validate-phase/SKILL.md` | Validate phase output |
| `btcedu/core/CLAUDE.md` | Core pipeline stage patterns |
| `btcedu/models/CLAUDE.md` | Model conventions |
| `btcedu/services/CLAUDE.md` | Service layer patterns |
| `btcedu/web/CLAUDE.md` | Web dashboard patterns |
| `btcedu/prompts/CLAUDE.md` | Prompt template conventions |
| `tests/CLAUDE.md` | Test patterns and fixtures |
| `docs/architecture/pipeline-overview.md` | Pipeline architecture |
| `docs/architecture/review-gate-flow.md` | Review gate flow |
| `docs/architecture/stock-asset-flow.md` | Stock asset flow |
| `docs/architecture/render-flow.md` | Render flow |
| `docs/decisions/001-stock-images-over-ai.md` | ADR: stock vs AI images |
| `docs/decisions/002-video-clips-opt-in.md` | ADR: video opt-in |
| `docs/decisions/003-granular-review-model.md` | ADR: granular review |
| `docs/decisions/004-sidecar-not-overwrite.md` | ADR: sidecar pattern |
| `docs/runbooks/run-pipeline.md` | Runbook: pipeline operation |
| `docs/runbooks/handle-review-gates.md` | Runbook: review gates |
| `docs/runbooks/debug-failed-episode.md` | Runbook: debugging |
| `docs/runbooks/smoke-test-video.md` | Runbook: video smoke test |
| `docs/runbooks/safe-recovery.md` | Runbook: recovery paths |
| `docs/architecture/claude-code-structure-plan.md` | This plan |
| `docs/architecture/claude-code-structure-implementation-report.md` | Final report |

### Update (2 files)

| File | Change |
|------|--------|
| `CLAUDE.md` | Rewrite: 459 → ~180 lines, move detailed reference to scoped files |
| `.claude/settings.local.json` | Clean up: remove embedded Caddyfile patterns, keep essentials |

---

## 6. Which CLAUDE.md Files Should Exist and Why

| File | Justification |
|------|---------------|
| `CLAUDE.md` (root) | Always loaded. Project overview, build commands, conventions, gotchas. ~180 lines. |
| `btcedu/core/CLAUDE.md` | Lazy-loaded when editing core/. Stage implementation pattern, cost guard, dry-run, cascade invalidation. |
| `btcedu/models/CLAUDE.md` | Lazy-loaded when editing models/. MediaAsset separate Base gotcha, enum values, Pydantic schema rules. |
| `btcedu/services/CLAUDE.md` | Lazy-loaded when editing services/. Protocol pattern, raw HTTP vs SDK, retry conventions. |
| `btcedu/web/CLAUDE.md` | Lazy-loaded when editing web/. Flask app factory, API endpoint conventions, SPA patterns. |
| `btcedu/prompts/CLAUDE.md` | Lazy-loaded when editing prompts/. Template format, frontmatter fields, Jinja2 patterns. |
| `tests/CLAUDE.md` | Lazy-loaded when editing tests/. Fixture patterns, db_session/db_engine, mock conventions, known gotchas. |

---

## 7. Which Agents Should Exist and Why

| Agent | Justification |
|-------|---------------|
| `pipeline-debug` | When a pipeline stage fails, this agent knows to check episode status, pipeline_run records, error_message, journal, and log output. Saves rediscovery time. |
| `review-workflow` | Manages review gates: creating tasks, checking approval status, understanding diff files, applying review decisions. Complex multi-table flow. |
| `stock-assets` | Handles stock image/video search, ranking, candidate selection, and finalization. Involves Pexels API, intent extraction, manifest files — a distinct subdomain. |

### Agents intentionally NOT created

| Idea | Why not |
|------|---------|
| `render-pipeline-agent` | Render is a single stage, not complex enough for a dedicated agent. |
| `docs-maintainer-agent` | Generic doc editing doesn't need specialization. |
| `test-regression-agent` | Running tests is a skill; investigating failures is covered by pipeline-debug. |

---

## 8. Which Skills Should Exist and Why

| Skill | Invocation | Justification |
|-------|------------|---------------|
| `deploy` | `/deploy` | Run `run.sh` safely, verify health check. Common operation. |
| `test` | `/test [pattern]` | Run pytest with optional pattern. Most frequent operation. |
| `smoke-test-video` | `/smoke-test-video` | Run `btcedu smoke-test-video`, check output. Pi-specific. |
| `pipeline-status` | `/pipeline-status` | Show episode status summary + pending reviews. Dashboard-like. |
| `investigate-failure` | `/investigate-failure <episode_id>` | Check episode error, pipeline runs, recent logs. |
| `validate-phase` | `/validate-phase` | Run full test suite + check test count against baseline. |

---

## 9. Whether .claude/commands/ Should Exist

**Decision: No.** Commands are legacy/deprecated in favor of skills.
Skills provide all the same functionality plus frontmatter, supporting files,
and auto-invocation. All workflows will be implemented as skills.

**Source:** Official Anthropic documentation confirms skills supersede commands.

---

## 10. Runbooks, Architecture, and Decision Docs to Create

### Architecture docs (docs/architecture/)

| Doc | Content |
|-----|---------|
| `pipeline-overview.md` | v1/v2 pipeline stages, status transitions, _V2_STAGES reference |
| `review-gate-flow.md` | How review gates work: create → pending → approve/reject → resume |
| `stock-asset-flow.md` | Search → rank → review → finalize → normalize video → manifest |
| `render-flow.md` | TTS + images → segments → concat → draft.mp4 |

### Decision records (docs/decisions/)

| ADR | Decision |
|-----|----------|
| `001-stock-images-over-ai.md` | Pexels stock images replaced DALL-E for realism + cost |
| `002-video-clips-opt-in.md` | Video B-roll is opt-in; photos are default fallback |
| `003-granular-review-model.md` | Per-item review with normalized ReviewItemAction/Decision |
| `004-sidecar-not-overwrite.md` | Review outputs to sidecar files, never overwrites pipeline artifacts |

### Runbooks (docs/runbooks/)

| Runbook | Audience |
|---------|----------|
| `run-pipeline.md` | Operator: how to run episodes through the pipeline |
| `handle-review-gates.md` | Operator: how to approve/reject at review gates |
| `debug-failed-episode.md` | Operator/Claude: systematic failure investigation |
| `smoke-test-video.md` | Operator: verify video pipeline on Raspberry Pi |
| `safe-recovery.md` | Operator: how to recover from common failure states |

---

## 11. Existing Docs to Preserve As-Is

| Doc | Reason |
|-----|--------|
| `MASTERPLAN.md` | Comprehensive reference (66KB). Too large for context but valuable for deep dives. |
| `README.md` | Project overview for humans/GitHub. |
| `CONTRIBUTING.md` | Contribution guidelines. |
| `docs/sprints/` (all 66 files) | Historical implementation record. |
| `docs/SETUP_GUIDE.md` | Setup instructions. |
| `docs/SERVER_DEPLOYMENT_GUIDE.md` | Deployment guide. |
| `docs/CONSTRAINTS_TABLE.md` | Performance constraints. |
| `docs/PROGRESS_LOG.md` | Project history. |
| `docs/UEBERSICHT.md` | German overview. |
| `docs/PROCESS_ALL_MULTI_CHANNEL.md` | Multi-channel guide. |
| `status.md` | Current status notes. |

---

## 12. Risks / Non-Goals / Migration Notes

### Risks
- **CLAUDE.md rewrite**: Removing detail means Claude must discover it from code. Mitigated by scoped files.
- **Agent quality**: Agents with vague instructions are worse than no agents. Each must be specific and tested.
- **Over-documentation**: Creating docs that immediately go stale. Mitigated by keeping them brief and structural.

### Non-goals
- Renaming source directories (btcedu/, tests/, deploy/)
- Moving or reorganizing sprint docs
- Rewriting MASTERPLAN.md
- Creating hooks (low value until specific need arises)
- Creating MCP server configurations
- Restructuring test files

### Migration notes
- `.claude/settings.json` (committed) will contain only model preference and safe permission patterns
- `.claude/settings.local.json` (gitignored) keeps personal/environment-specific allows
- Root `CLAUDE.md` will use `@docs/architecture/pipeline-overview.md` references for deep dives
- Scoped `CLAUDE.md` files reference parent without duplicating it

### Ideas from reference image intentionally not copied
- Separate `.claude/rules/` directory (scoped CLAUDE.md achieves the same with official support)
- `.claude/hooks/` directory (hooks belong in settings.json per official convention)
- Complex agent orchestration chains (this project doesn't need them yet)
- Memory restructuring (current memory system works; it's machine-local and not committed)
