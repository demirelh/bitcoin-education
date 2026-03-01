# btcedu Project Memory

## Notes

- Project context, architecture, patterns, and file map are now in `/home/pi/AI-Startup-Lab/bitcoin-education/CLAUDE.md`
- This file tracks session-specific learnings not covered there

## Debugging Insights

- When adding new stages: remember to add status to `run_pending()` and `run_latest()` filter lists
- `_run_stage()` cost extraction parses `$` from StageResult.detail — ensure new stages include `$X.XXXX` in detail string
- ElevenLabsService is lazy-imported inside `generate_tts()` — mock at source (`btcedu.services.elevenlabs_service.ElevenLabsService`), not at usage site

## Test Date
- Sprint 8 complete: 476 tests passing (2026-03-01)
