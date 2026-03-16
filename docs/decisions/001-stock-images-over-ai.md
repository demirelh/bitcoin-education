# ADR-001: Stock Images Over AI-Generated Images

**Date:** 2026-02 (Phase 3)
**Status:** Accepted
**Supersedes:** Original DALL-E 3 image generation

## Context

The original pipeline (Sprint 7) used DALL-E 3 to generate per-chapter images. This produced stylistically inconsistent results, occasionally hallucinated visual content, and cost ~$0.04-0.08 per image.

## Decision

Replace AI-generated images with Pexels stock photos as the default visual asset source, using LLM-based smart ranking to select the most relevant candidate per chapter.

## Rationale

- **Realism**: stock photos look natural and professional for educational content
- **Consistency**: Pexels photos have consistent quality and licensing (free for commercial use)
- **Cost**: Pexels API is free; only the LLM ranking step has cost (~$0.003 per chapter)
- **Control**: human review gate allows manual selection before finalization
- **Fallback**: DALL-E 3 path still exists in `image_generator.py` if needed

## Consequences

- Added Pexels service (`pexels_service.py`), smart ranking with intent extraction
- Review gate added between candidate selection and finalization
- Placeholder fallback for chapters where no good candidate exists
- Video B-roll support added later (ADR-002) as an extension of this approach
