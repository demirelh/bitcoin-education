# btcedu Architecture Documentation

**Version:** 1.0
**Last Updated:** February 2026
**Audience:** Software engineers onboarding to the project

This document provides a comprehensive technical overview of the btcedu system — an automated content localization pipeline that transforms German Bitcoin podcast episodes into Turkish educational YouTube packages.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Data Flow End-to-End](#3-data-flow-end-to-end)
4. [Repository Structure](#4-repository-structure)
5. [Database Schema](#5-database-schema)
6. [Pipeline & Job System](#6-pipeline--job-system)
7. [Web Dashboard Architecture](#7-web-dashboard-architecture)
8. [CLI Commands](#8-cli-commands)
9. [Deployment Architecture](#9-deployment-architecture)
10. [How to Extend the System](#10-how-to-extend-the-system)
11. [Development Workflow](#11-development-workflow)

---

## 1. Project Overview

### What is btcedu?

**btcedu** is an automated content localization pipeline that transforms German Bitcoin podcast episodes into Turkish educational YouTube content packages. It processes podcast episodes through a series of stages — from audio download to AI-generated content creation — and produces publication-ready materials.

**Source Material:** "Der Bitcoin Podcast" by Florian Bruce Boye (YouTube)

**Output:** 6 Turkish content artifacts per episode:
- Episode outline with citations
- Long-form video script (12-15 minutes)
- Short-form video scripts (6 × TikTok/Reels)
- Visual/slide descriptions
- Q&A pairs for community engagement
- Publishing package (titles, descriptions, tags)

### Problem It Solves

Creating localized educational content manually is:
1. **Time-intensive:** Transcription, translation, and content adaptation require hours per episode
2. **Expensive:** Professional translation and content creation services are costly
3. **Inconsistent:** Manual processes lead to varying quality and style
4. **Slow to scale:** Cannot keep pace with new episode releases

btcedu automates this workflow, reducing per-episode processing from hours to minutes (plus API time), at a cost of ~$0.38 per episode.

### High-Level Workflow

```
YouTube Episode → Audio Download → Transcription (Whisper)
    → Chunking + FTS5 Indexing → Content Generation (Claude RAG)
    → 6 Turkish Artifacts (outline, script, shorts, visuals, Q&A, publishing)
```

### Why RAG + Automation Pipeline?

**Retrieval-Augmented Generation (RAG)** ensures:
- **Accuracy:** Claude generates content based on actual transcript chunks, not hallucinations
- **Citations:** Every claim can be traced back to specific parts of the original episode
- **Contextual relevance:** Search retrieves the most relevant chunks for each content type

**Automation Pipeline** provides:
- **Idempotency:** Stages can be re-run safely without duplicating work
- **Error recovery:** Failed episodes can be retried from the last successful stage
- **Cost tracking:** Every API call is logged with token usage and estimated cost
- **Scalability:** Process entire channel backlogs with batch operations

---

## 2. High-Level Architecture

### System Components

```
┌────────────────────────────────────────────────────────────────────┐
│                         User Interface Layer                        │
├──────────────────────────┬─────────────────────────────────────────┤
│   CLI (cli.py)          │   Web Dashboard (Flask)                  │
│   - detect              │   - Episode management UI                │
│   - download            │   - Job control & monitoring             │
│   - transcribe          │   - Batch processing                     │
│   - chunk               │   - Cost analytics                       │
│   - generate            │   - Content viewer                       │
│   - run / retry         │   - Multi-channel support                │
└──────────────────────────┴─────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                        Application Layer                            │
├─────────────────────┬────────────────────────┬─────────────────────┤
│  Pipeline Core      │  Background Jobs       │  Services Layer     │
│  core/             │  web/jobs.py           │  services/          │
│  ├─ detector.py    │  ├─ JobManager         │  ├─ feed_service    │
│  ├─ transcriber.py │  ├─ Single jobs        │  ├─ download_svc    │
│  ├─ chunker.py     │  └─ Batch processor    │  ├─ whisper_svc     │
│  ├─ generator.py   │                        │  └─ claude_svc      │
│  └─ pipeline.py    │                        │                     │
└─────────────────────┴────────────────────────┴─────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                         Data Layer                                  │
├────────────────────────────────────────────────────────────────────┤
│  Database (SQLite + FTS5)          File System                     │
│  ├─ episodes                       data/                           │
│  ├─ pipeline_runs                  ├─ raw/         (audio)         │
│  ├─ chunks + chunks_fts            ├─ transcripts/ (German text)   │
│  ├─ content_artifacts              ├─ chunks/      (JSONL)         │
│  ├─ channels                       ├─ outputs/     (6 artifacts)   │
│  ├─ batch_runs                     ├─ reports/     (JSON reports)  │
│  └─ schema_migrations              └─ logs/        (per-episode)   │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                      External Services                              │
├──────────────┬──────────────┬──────────────┬──────────────────────┤
│ YouTube RSS  │ yt-dlp       │ OpenAI       │ Anthropic Claude     │
│ (detection)  │ (download)   │ (Whisper)    │ (content gen)        │
└──────────────┴──────────────┴──────────────┴──────────────────────┘
```

### Component Interactions

1. **CLI/Web Dashboard** → Initiates pipeline operations via core modules
2. **Pipeline Core** → Orchestrates stage execution, manages episode state
3. **Services Layer** → Abstracts external API calls (Whisper, Claude, yt-dlp)
4. **Database** → Stores all metadata, search index, and execution history
5. **File System** → Stores binary/text artifacts (audio, transcripts, outputs)
6. **JobManager** → Queues and executes background tasks (web dashboard only)

### Key Architectural Decisions

| Decision | Rationale |
|----------|-----------|
| **SQLite with FTS5** | No external database needed; full-text search built-in; perfect for single-writer workloads |
| **Idempotent stages** | Safe to re-run; outputs compared by prompt hash to avoid redundant API calls |
| **Single-threaded job execution** | Respects SQLite's single-writer constraint; jobs queue sequentially |
| **Stateless pipeline stages** | Each stage reads from DB/disk, processes, writes results; no in-memory coupling |
| **Dry-run mode** | Test entire pipeline without API calls; preview prompts and costs |
| **File-based storage** | Audio/transcripts/outputs stored as files; DB stores only metadata and pointers |

---

## 3. Data Flow End-to-End

### Episode Lifecycle Status Progression

```
NEW → DOWNLOADED → TRANSCRIBED → CHUNKED → GENERATED → REFINED → COMPLETED
                                                            ↓
                                                         FAILED (on error)
```

Each episode transitions through these statuses as it progresses through the pipeline. Stages are skipped if already completed (unless `--force` is used).

### Full Pipeline Flow

#### Stage 1: Channel Detection

**Command:** `btcedu detect`

**Process:**
1. Fetch RSS feed from YouTube channel or custom RSS URL
2. Parse feed entries (title, URL, published date, duration)
3. For each entry, check if episode exists in database
4. Insert new episodes with status `NEW`

**Inputs:** RSS feed URL (from config)
**Outputs:** Episode records in database
**Storage:** Database table `episodes`

#### Stage 2: Episode Detection (Backfill)

**Command:** `btcedu backfill`

**Process:**
1. Run `yt-dlp --flat-playlist -J <channel_url>` to list all videos
2. Parse JSON output for video IDs, titles, upload dates
3. Apply filters (--since, --until, --max)
4. Insert new episodes with status `NEW`, source `youtube_backfill`

**Inputs:** YouTube channel ID
**Outputs:** Episode records in database
**Storage:** Database table `episodes`

#### Stage 3: Audio Download

**Command:** `btcedu download --episode-id <ID>`

**Process:**
1. Load episode record from database
2. Call yt-dlp to extract audio: `yt-dlp -f bestaudio <url> -o <path>`
3. Save audio to `data/raw/{episode_id}/audio.m4a`
4. Update episode: `status = DOWNLOADED`, `audio_path = <path>`

**Inputs:** Episode URL
**Outputs:** Audio file (M4A format)
**Storage:** `data/raw/{episode_id}/audio.m4a`

**API Calls:** None (yt-dlp is local)

#### Stage 4: Transcription

**Command:** `btcedu transcribe --episode-id <ID>`

**Process:**
1. Load episode record and read audio file
2. If audio > 24MB: split into chunks via pydub, transcribe each, concatenate
3. Send to OpenAI Whisper API with `language=de`
4. Save raw transcript to `data/transcripts/{episode_id}/transcript.de.txt`
5. Clean transcript (remove timestamps, artifacts)
6. Save cleaned transcript to `data/transcripts/{episode_id}/transcript.clean.de.txt`
7. Update episode: `status = TRANSCRIBED`, `transcript_path = <path>`
8. Record PipelineRun with token usage and cost

**Inputs:** Audio file
**Outputs:** German transcript (raw + cleaned)
**Storage:** `data/transcripts/{episode_id}/`

**API Calls:** OpenAI Whisper API

#### Stage 5: Chunking + FTS5 Indexing

**Command:** `btcedu chunk --episode-id <ID>`

**Process:**
1. Load episode and read cleaned transcript
2. Split into overlapping chunks:
   - Default size: 1500 characters (~350 tokens)
   - Overlap: 15%
   - Try to break at sentence boundaries
3. Create Chunk records with `chunk_id = {episode_id}_{ordinal:03d}`
4. Estimate tokens (~4 chars per token)
5. Write chunks to `data/chunks/{episode_id}/chunks.jsonl`
6. Insert chunks into database `chunks` table
7. Insert into FTS5 virtual table `chunks_fts` for full-text search
8. Update episode: `status = CHUNKED`

**Inputs:** Cleaned transcript
**Outputs:** Chunk records + FTS5 search index
**Storage:** Database tables `chunks` + `chunks_fts`, plus JSONL backup

**API Calls:** None

#### Stage 6: Content Generation

**Command:** `btcedu generate --episode-id <ID>`

**Process:**
For each of 6 content artifacts (outline, script, shorts, visuals, Q&A, publishing):

1. **Retrieve relevant chunks:**
   - Build search query from episode title (filter German stopwords)
   - Query FTS5 index: `SELECT * FROM chunks_fts WHERE chunks_fts MATCH <query> LIMIT 16`
   - Fallback to first 16 chunks if search returns < 8 results

2. **Build prompt:**
   - System prompt: Turkish content rules, disclaimers, formatting
   - User prompt: Task description + retrieved chunks (with citation IDs) + previous artifacts
   - Calculate prompt hash (SHA256) for idempotency

3. **Call Claude API:**
   - Model: `claude-sonnet-4-20250514`
   - Max tokens: 4096
   - Temperature: 0.3
   - Track input/output tokens

4. **Save artifact:**
   - Write to `data/outputs/{episode_id}/{artifact_name}`
   - Record ContentArtifact with prompt hash
   - Record PipelineRun with cost

5. **Repeat for remaining artifacts** (each artifact can reference previous ones)

6. Update episode: `status = GENERATED`

**Inputs:** Chunks (from FTS5), episode metadata, previous artifacts
**Outputs:** 6 Turkish content files
**Storage:** `data/outputs/{episode_id}/` + database records

**API Calls:** 6 × Claude API calls (~$0.38 total)

**Artifacts Generated:**
- `outline.tr.md` - Episode outline with citations
- `script.long.tr.md` - Full YouTube video script
- `shorts.tr.json` - 6 short-form video scripts
- `visuals.json` - Visual/slide descriptions
- `qa.json` - Q&A pairs
- `publishing_pack.json` - Titles, descriptions, tags, thumbnails

#### Stage 7: Refinement (Optional)

**Command:** `btcedu refine --episode-id <ID>`

**Process:**
1. Load existing artifacts (outline, script, publishing)
2. Call Claude API with refinement prompts:
   - Improve clarity, flow, accuracy
   - Polish language and style
   - Fix any inconsistencies
3. Save refined versions with `.v2` suffix
4. Update episode: `status = REFINED`

**Inputs:** Generated artifacts
**Outputs:** Refined versions (v2 files)
**Storage:** `data/outputs/{episode_id}/` with `.v2` suffix

**API Calls:** 3 × Claude API calls

#### Stage 8: Report Generation

**Command:** Automatic during pipeline execution

**Process:**
1. Collect all PipelineRun records for episode
2. Aggregate:
   - Stage-by-stage timeline
   - Total input/output tokens
   - Estimated cost breakdown
   - Error messages (if any)
3. Save JSON report

**Inputs:** PipelineRun records from database
**Outputs:** JSON report
**Storage:** `data/reports/{episode_id}/report-{timestamp}.json`

#### Stage 9: Batch Processing

**Command:** `btcedu run-pending --max N --since YYYY-MM-DD` (CLI)
**Web Dashboard:** "Process All" button

**Process:**
1. Query database for episodes with status < GENERATED
2. Filter by channel (if specified) and date (if specified)
3. Limit to N episodes (if specified)
4. For each episode sequentially:
   - Run full pipeline from current status
   - Update batch progress (% complete, current episode)
   - Check stop flag between episodes
   - Aggregate costs
5. Update batch status: `success` or `error`

**Inputs:** Episode IDs from database query
**Outputs:** Processed episodes + batch report
**Storage:** Database table `batch_runs` (web dashboard only)

**Graceful Stop:** Stop flag checked between episodes; current episode completes before stopping

---

## 4. Repository Structure

```
bitcoin-education/
│
├── btcedu/                        # Main Python package
│   ├── __init__.py
│   ├── cli.py                     # Click CLI entry point (all commands)
│   ├── config.py                  # Pydantic settings (from .env)
│   ├── db.py                      # SQLAlchemy engine/session + FTS5 init
│   │
│   ├── core/                      # Pipeline stage implementations
│   │   ├── __init__.py
│   │   ├── detector.py            # RSS/YouTube feed + backfill + download
│   │   ├── transcriber.py         # Whisper transcription + cleaning
│   │   ├── chunker.py             # Text chunking + FTS5 indexing
│   │   ├── generator.py           # Claude content generation orchestration
│   │   └── pipeline.py            # Pipeline state machine + error recovery
│   │
│   ├── services/                  # External service integrations
│   │   ├── __init__.py
│   │   ├── feed_service.py        # RSS/Atom parsing + YouTube feed
│   │   ├── download_service.py    # yt-dlp wrapper for audio extraction
│   │   ├── transcription_service.py # Whisper API + file splitting
│   │   └── claude_service.py      # Claude API wrapper + cost calc
│   │
│   ├── models/                    # SQLAlchemy ORM + Pydantic schemas
│   │   ├── __init__.py
│   │   ├── episode.py             # Episode, PipelineRun, Chunk models
│   │   ├── content_artifact.py    # ContentArtifact model
│   │   ├── channel.py             # Channel model (multi-channel)
│   │   ├── migration.py           # SchemaMigration model
│   │   └── schemas.py             # Pydantic validation schemas
│   │
│   ├── migrations/                # Database schema migrations
│   │   ├── __init__.py            # Migration registry + execution
│   │   └── 001_add_channels_support.py
│   │
│   ├── prompts/                   # Claude prompt templates
│   │   ├── __init__.py
│   │   ├── system.py              # Shared Turkish system prompt
│   │   ├── outline.py             # Episode outline generation
│   │   ├── script.py              # Long-form video script
│   │   ├── shorts.py              # Short-form video scripts
│   │   ├── visuals.py             # Visual/slide descriptions
│   │   ├── qa.py                  # Q&A pairs
│   │   ├── publishing.py          # Titles, descriptions, tags
│   │   ├── refine_outline.py      # Outline refinement
│   │   └── refine_script.py       # Script refinement
│   │
│   ├── utils/                     # Utility functions
│   │   ├── __init__.py
│   │   ├── journal.py             # Progress log utility
│   │   └── llm_introspection.py   # LLM usage report generation
│   │
│   └── web/                       # Flask web dashboard
│       ├── __init__.py
│       ├── app.py                 # Flask app factory + config
│       ├── api.py                 # REST API endpoints (~20 routes)
│       ├── jobs.py                # JobManager + batch processor
│       ├── templates/             # Jinja2 HTML templates
│       │   └── index.html
│       └── static/                # JavaScript + CSS
│           ├── app.js             # State management + API client
│           └── style.css          # Responsive layout
│
├── deploy/                        # Production deployment files
│   ├── btcedu-detect.service      # systemd service (detect)
│   ├── btcedu-detect.timer        # systemd timer (every 6h)
│   ├── btcedu-run.service         # systemd service (run-pending)
│   ├── btcedu-run.timer           # systemd timer (daily 02:00)
│   ├── btcedu-web.service         # systemd service (gunicorn)
│   ├── Caddyfile.dashboard        # Caddy reverse proxy config
│   └── setup-web.sh               # Web deployment helper
│
├── tests/                         # Pytest test suite
│   ├── conftest.py                # Pytest fixtures
│   ├── test_config.py
│   ├── test_detector.py
│   ├── test_chunker.py
│   ├── test_transcriber.py
│   ├── test_generator.py
│   ├── test_pipeline.py
│   ├── test_models.py
│   ├── test_migrations.py
│   ├── test_web.py
│   ├── test_journal.py
│   └── fixtures/                  # Test data
│
├── docs/                          # Documentation
│   ├── ARCHITECTURE.md            # This file
│   ├── PROGRESS_LOG.md            # Append-only dev journal
│   └── PROCESS_ALL_MULTI_CHANNEL.md # Feature docs
│
├── data/                          # Runtime data (created on first run)
│   ├── btcedu.db                  # SQLite database
│   ├── raw/                       # Downloaded audio files
│   │   └── {episode_id}/
│   │       └── audio.m4a
│   ├── transcripts/               # Whisper transcripts
│   │   └── {episode_id}/
│   │       ├── transcript.de.txt
│   │       └── transcript.clean.de.txt
│   ├── chunks/                    # Chunk JSONL files
│   │   └── {episode_id}/
│   │       └── chunks.jsonl
│   ├── outputs/                   # Generated content artifacts
│   │   └── {episode_id}/
│   │       ├── outline.tr.md
│   │       ├── script.long.tr.md
│   │       ├── shorts.tr.json
│   │       ├── visuals.json
│   │       ├── qa.json
│   │       ├── publishing_pack.json
│   │       ├── outline.tr.v2.md        # Optional refined
│   │       ├── script.long.tr.v2.md    # versions
│   │       └── publishing_pack.v2.json
│   ├── reports/                   # Pipeline execution reports
│   │   └── {episode_id}/
│   │       └── report-{timestamp}.json
│   └── logs/                      # Application logs
│       ├── web.log
│       ├── web_errors.log
│       └── episodes/
│           └── {episode_id}.log
│
├── .github/                       # GitHub Actions CI/CD
│   └── workflows/
│       ├── ci.yml                 # Linting + tests
│       ├── deploy.yml             # Deployment to production
│       └── security.yml           # Security scanning
│
├── README.md                      # Main documentation
├── pyproject.toml                 # Python package config
├── .env.example                   # Environment template
├── .gitignore
└── LICENSE
```

### Module Responsibilities

| Module | Responsibility |
|--------|----------------|
| **btcedu/core/** | Pipeline stage implementations (detect, download, transcribe, chunk, generate) |
| **btcedu/services/** | External API wrappers (Whisper, Claude, yt-dlp, RSS) |
| **btcedu/models/** | Database models (SQLAlchemy ORM) + validation schemas (Pydantic) |
| **btcedu/prompts/** | Claude prompt templates for each content artifact |
| **btcedu/web/** | Flask web dashboard (API + UI) |
| **btcedu/utils/** | Utilities (journal logging, etc.) |
| **btcedu/migrations/** | Database schema migrations |

---

## 5. Database Schema

btcedu uses SQLite with FTS5 full-text search extension. All metadata and search indexes are stored in `data/btcedu.db`.

### Entity Relationship Diagram

```
┌─────────────────┐          ┌──────────────────┐
│    channels     │          │    episodes      │
├─────────────────┤          ├──────────────────┤
│ id (PK)         │          │ id (PK)          │
│ channel_id (UK) │◄────────┤ channel_id (FK)  │
│ name            │          │ episode_id (UK)  │
│ youtube_ch_id   │          │ source           │
│ rss_url         │          │ title            │
│ is_active       │          │ published_at     │
│ created_at      │          │ duration_seconds │
│ updated_at      │          │ url              │
└─────────────────┘          │ status (Enum)    │
                             │ audio_path       │
                             │ transcript_path  │
                             │ output_dir       │
                             │ detected_at      │
                             │ completed_at     │
                             │ error_message    │
                             │ retry_count      │
                             └────────┬─────────┘
                                      │
                       ┌──────────────┼──────────────┐
                       │              │              │
                       ▼              ▼              ▼
            ┌──────────────┐  ┌──────────┐  ┌──────────────────┐
            │pipeline_runs │  │  chunks  │  │content_artifacts │
            ├──────────────┤  ├──────────┤  ├──────────────────┤
            │ id (PK)      │  │ id (PK)  │  │ id (PK)          │
            │ episode_id   │  │ chunk_id │  │ episode_id       │
            │ stage (Enum) │  │ episode_ │  │ artifact_type    │
            │ status       │  │   id     │  │ file_path        │
            │ started_at   │  │ ordinal  │  │ model            │
            │ completed_at │  │ text     │  │ prompt_hash      │
            │ input_tokens │  │ token_   │  │ retrieval_snap   │
            │ output_tokens│  │   est    │  │ created_at       │
            │ cost_usd     │  │ start_   │  └──────────────────┘
            │ error_msg    │  │   char   │
            └──────────────┘  │ end_char │
                             └─────┬────┘
                                   │
                                   ▼
                         ┌──────────────────┐
                         │   chunks_fts     │
                         │  (FTS5 Virtual)  │
                         ├──────────────────┤
                         │ rowid            │
                         │ text             │
                         └──────────────────┘
```

### Table Schemas

#### episodes

Stores all detected podcast episodes and tracks their pipeline status.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `episode_id` | VARCHAR(64) UNIQUE | YouTube video ID or hash |
| `channel_id` | VARCHAR(64) | Channel identifier (FK to channels) |
| `source` | VARCHAR(32) | "youtube_rss", "youtube_backfill", or "rss" |
| `title` | VARCHAR(500) | Episode title |
| `published_at` | TIMESTAMP | Publication date |
| `duration_seconds` | INTEGER | Video duration |
| `url` | VARCHAR(500) | Episode URL |
| `status` | ENUM | NEW, DOWNLOADED, TRANSCRIBED, CHUNKED, GENERATED, REFINED, COMPLETED, FAILED |
| `audio_path` | VARCHAR(500) | Path to downloaded audio file |
| `transcript_path` | VARCHAR(500) | Path to cleaned transcript |
| `output_dir` | VARCHAR(500) | Path to generated outputs directory |
| `detected_at` | TIMESTAMP | When episode was first discovered |
| `completed_at` | TIMESTAMP | When pipeline completed |
| `error_message` | TEXT | Last error if status = FAILED |
| `retry_count` | INTEGER | Number of retry attempts |

**Indexes:** `episode_id` (unique), `channel_id`, `status`

**Relationships:**
- One-to-many with `pipeline_runs`
- One-to-many with `chunks` (via `episode_id` string)
- One-to-many with `content_artifacts` (via `episode_id` string)

#### pipeline_runs

Audit trail of all pipeline stage executions (used for cost tracking and debugging).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `episode_id` | INTEGER | FK to episodes.id |
| `stage` | ENUM | DETECT, DOWNLOAD, TRANSCRIBE, CHUNK, GENERATE, REFINE, COMPLETE |
| `status` | ENUM | RUNNING, SUCCESS, FAILED |
| `started_at` | TIMESTAMP | When stage started |
| `completed_at` | TIMESTAMP | When stage completed |
| `input_tokens` | INTEGER | Claude input tokens used (0 for non-Claude stages) |
| `output_tokens` | INTEGER | Claude output tokens used |
| `estimated_cost_usd` | FLOAT | Estimated cost for this run |
| `error_message` | TEXT | Error details if failed |

**Indexes:** `episode_id`

**Relationships:** Many-to-one with `episodes`

**Why this exists:** Tracks every pipeline execution for cost analysis, performance monitoring, and debugging failed runs.

#### chunks

Stores overlapping text chunks from transcripts for retrieval-augmented generation.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `chunk_id` | VARCHAR(64) UNIQUE | `{episode_id}_{ordinal:03d}` |
| `episode_id` | VARCHAR(64) | Episode identifier (matches episodes.episode_id) |
| `ordinal` | INTEGER | Chunk sequence number (0-indexed) |
| `text` | TEXT | Chunk text content |
| `token_estimate` | INTEGER | Estimated tokens (~4 chars per token) |
| `start_char` | INTEGER | Character offset in original transcript |
| `end_char` | INTEGER | Character offset end |

**Indexes:** `chunk_id` (unique), `episode_id`

**Relationships:** Many-to-one with `episodes` (via string `episode_id`)

**Why this exists:** Enables efficient retrieval of relevant transcript segments for Claude prompts, supporting citation-based content generation.

#### chunks_fts (Virtual FTS5 Table)

Full-text search index over the `chunks` table for fast keyword-based retrieval.

| Column | Type | Description |
|--------|------|-------------|
| `rowid` | INTEGER | Maps to chunks.id |
| `text` | TEXT | Indexed text (from chunks.text) |

**Usage:** `SELECT * FROM chunks_fts WHERE chunks_fts MATCH '<query>' ORDER BY rank LIMIT 16`

**Why this exists:** SQLite's FTS5 provides fast, relevant full-text search without external dependencies (no vector DB needed).

#### content_artifacts

Tracks generated content files and their generation metadata (for idempotency and versioning).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `episode_id` | VARCHAR(64) | Episode identifier |
| `artifact_type` | VARCHAR(32) | "outline", "script", "shorts", "visuals", "qa", "publishing" |
| `file_path` | VARCHAR(500) | Path to generated file |
| `model` | VARCHAR(64) | Claude model used |
| `prompt_hash` | VARCHAR(64) | SHA256 of system + user prompt |
| `retrieval_snapshot_path` | VARCHAR(500) | Path to retrieved chunks JSON (optional) |
| `created_at` | TIMESTAMP | Generation timestamp |

**Indexes:** `episode_id`, `artifact_type`

**Relationships:** Many-to-one with `episodes` (via string `episode_id`)

**Why this exists:** Prevents regenerating artifacts with identical prompts (idempotency), tracks which model version created each artifact, and enables prompt versioning.

#### channels

Manages multiple YouTube channels or RSS feeds (multi-channel support).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `channel_id` | VARCHAR(64) UNIQUE | Channel identifier (slug) |
| `name` | VARCHAR(200) | Human-readable channel name |
| `youtube_channel_id` | VARCHAR(64) | YouTube channel ID |
| `rss_url` | VARCHAR(500) | Custom RSS URL (alternative to YouTube) |
| `is_active` | BOOLEAN | Whether to process this channel |
| `created_at` | TIMESTAMP | When channel was added |
| `updated_at` | TIMESTAMP | Last update |

**Indexes:** `channel_id` (unique)

**Relationships:** One-to-many with `episodes`

**Why this exists:** Allows tracking multiple podcast sources, filtering episodes by channel, and toggling channels on/off.

#### schema_migrations

Tracks applied database schema migrations (for version control).

| Column | Type | Description |
|--------|------|-------------|
| `version` | VARCHAR(50) PRIMARY KEY | Migration version (e.g., "001_add_channels_support") |
| `applied_at` | TIMESTAMP | When migration was applied |

**Why this exists:** Ensures migrations are applied exactly once, enables safe schema evolution, and prevents running old code against new schema.

---

## 6. Pipeline & Job System

### Pipeline Orchestration

The pipeline is orchestrated by `btcedu/core/pipeline.py`, which implements a state machine that:
1. Determines which stages need to run based on episode status
2. Executes stages in order: download → transcribe → chunk → generate
3. Updates episode status after each stage
4. Records PipelineRun entries for audit
5. Handles errors and sets status to FAILED

### Idempotency & Force Mode

**Idempotent by default:** Running a stage on an episode that's already past that stage does nothing.

Example: Running `btcedu transcribe` on an episode with status `CHUNKED` skips transcription.

**Force mode:** `--force` flag or force checkbox (web UI) bypasses idempotency checks and re-runs the stage even if output exists.

**Prompt-based idempotency:** During generation, if an artifact file exists AND the prompt hash matches the stored hash in `content_artifacts`, Claude is not called (output reused).

### Retry Semantics

**Retry behavior:** `btcedu retry --episode-id <ID>` is used to resume from failures.

Requirements:
1. Episode must have `status = FAILED` or have an `error_message`
2. Retry clears the error message
3. Pipeline resumes from the current status (last successful stage)

Example:
- Episode at `CHUNKED` with error "Claude API timeout during generate"
- `btcedu retry` clears error, skips download/transcribe/chunk, re-runs generate

**vs run:** `btcedu run` always runs from the first incomplete stage, regardless of errors. Use `run` for normal processing, `retry` for error recovery.

### Single Episode Jobs (CLI)

| Command | Description | Idempotent |
|---------|-------------|-----------|
| `btcedu run --episode-id <ID>` | Run all stages from first incomplete | Yes |
| `btcedu retry --episode-id <ID>` | Retry from last failed stage | Yes |
| `btcedu download --episode-id <ID>` | Download audio only | Yes |
| `btcedu transcribe --episode-id <ID>` | Transcribe only | Yes |
| `btcedu chunk --episode-id <ID>` | Chunk only | Yes |
| `btcedu generate --episode-id <ID>` | Generate content only | Yes |

All commands support `--force` to bypass idempotency.

### Batch Jobs (Process All)

**CLI:** `btcedu run-pending [--max N] [--since YYYY-MM-DD] [--channel-id ID]`

**Web Dashboard:** "Process All" button

**Process:**
1. Query episodes with status < GENERATED
2. Filter by channel (if specified), date (if specified)
3. Limit to N episodes (if specified)
4. For each episode sequentially:
   - Run full pipeline from current status
   - Update progress (% complete)
   - Check stop flag between episodes
   - Aggregate costs

**Graceful Stop:**
- Web dashboard: "Stop" button sets `stop_requested = True`
- JobManager checks flag between episodes
- Current episode completes before stopping
- Batch status set to `stopped`

**Progress Tracking:**
- Web dashboard polls `/api/batch/<batch_id>` every 2 seconds
- Shows progress bar, current episode, completed/total counts
- Real-time cost accumulation

### Background Job System (Web Only)

**Implementation:** `btcedu/web/jobs.py`

**JobManager:**
- Uses `ThreadPoolExecutor` with `max_workers=1` (single-threaded)
- Jobs queue and execute sequentially (respects SQLite single-writer constraint)
- Jobs stored in-memory (lost on restart, but DB status is source of truth)

**Job States:**
```
queued → running → [success | error]
```

**Job Types:**
- Single-episode jobs: download, transcribe, chunk, generate, run, retry
- Batch jobs: process all pending episodes

**Job Logs:**
- Per-episode logs written to `data/logs/episodes/{episode_id}.log`
- Web dashboard streams logs in real-time via polling

---

## 7. Web Dashboard Architecture

### Technology Stack

| Component | Technology |
|-----------|------------|
| Backend | Flask 3.0+ |
| Frontend | Vanilla JavaScript (ES6+) |
| Styling | Custom CSS (responsive, mobile-first) |
| API | REST (JSON) |
| Background Jobs | ThreadPoolExecutor (single-threaded) |

### Flask App Structure

**App Factory:** `btcedu/web/app.py`

```python
def create_app(settings):
    app = Flask(__name__)
    app.config.from_object(settings)

    # Initialize JobManager
    job_manager = JobManager(logs_dir=settings.logs_dir)
    app.job_manager = job_manager

    # Register API blueprint
    from btcedu.web.api import create_api_blueprint
    app.register_blueprint(create_api_blueprint(job_manager))

    return app
```

### REST API Endpoints

**Health & Debug:**
- `GET /api/health` - Health check (returns `{"status": "ok"}`)
- `GET /api/debug/db-schema` - Database schema introspection

**Episode Management:**
- `GET /api/episodes` - List all episodes (supports filters: `status`, `channel_id`, `since`)
- `GET /api/episodes/<episode_id>` - Episode details + file presence indicators
- `GET /api/episodes/<episode_id>/files/<file_type>` - Retrieve generated file content

**Pipeline Control:**
- `POST /api/episodes/<episode_id>/download` - Queue download job
- `POST /api/episodes/<episode_id>/transcribe` - Queue transcribe job
- `POST /api/episodes/<episode_id>/chunk` - Queue chunk job
- `POST /api/episodes/<episode_id>/generate` - Queue generate job (supports `force`, `dry_run`, `top_k`)
- `POST /api/episodes/<episode_id>/refine` - Queue refinement job (supports `force`)
- `POST /api/episodes/<episode_id>/run` - Queue full pipeline
- `POST /api/episodes/<episode_id>/retry` - Queue retry
- `POST /api/detect` - Run detection (check feed for new episodes)

**Batch Processing:**
- `POST /api/batch/start` - Start batch processing (supports `force`, `channel_id`)
- `GET /api/batch/<batch_id>` - Check batch status (progress, current episode, costs)
- `POST /api/batch/<batch_id>/stop` - Gracefully stop batch
- `GET /api/batch/active` - Check if any batch is running

**Monitoring:**
- `GET /api/jobs/<job_id>` - Get job status + logs
- `GET /api/cost` - Aggregated cost summary (by episode, by channel)
- `GET /api/whats-new` - Dashboard summary (new, failed, pending counts)
- `GET /api/episodes/<episode_id>/action-log` - Job history for episode

**Channel Management:**
- `GET /api/channels` - List all channels
- `POST /api/channels` - Create a new channel (requires `name` + `youtube_channel_id` or `rss_url`)
- `DELETE /api/channels/<id>` - Delete a channel (blocked if episodes exist)
- `POST /api/channels/<id>/toggle` - Toggle channel active/inactive status

### Frontend Architecture (SPA)

**Single-Page Application:** `btcedu/web/static/app.js`

**State Management:**
```javascript
const state = {
    episodes: [],
    selectedEpisode: null,
    jobs: {},
    batchJob: null,
    filters: { status: 'all', channel: 'all' }
};
```

**Key Features:**
1. **Episode List:** Filterable table with status badges, file indicators, action buttons
2. **Episode Detail:** Tabbed content viewer (transcript, outline, script, QA, publishing, report)
3. **Real-time Updates:** Polls `/api/jobs/<job_id>` every 2 seconds while jobs run
4. **Batch Progress:** Progress bar with current episode, % complete, cost accumulation
5. **Mobile-Responsive:** Flex layout, viewport-aware heights, momentum scrolling

**Polling Strategy:**
- Job polling: Every 2 seconds while `state === 'running'`
- Batch polling: Every 2 seconds while batch active
- Stops polling when job/batch completes

### Mobile vs Desktop Layout

**Desktop:**
- Episode list on left (40% width)
- Episode detail on right (60% width)
- Side-by-side layout

**Mobile (< 768px):**
- Stacked layout (episode list above detail)
- Full-width panels
- Episode detail hides list when active
- "Back to list" button to return

**Scroll Behavior:**
- Each panel scrolls independently
- Fixed heights with `overflow-y: auto`
- Momentum scrolling (`-webkit-overflow-scrolling: touch`)
- Viewport-aware heights (`vh` units adjusted for iOS Safari)

### Job Polling + Log Streaming

**Job Polling:**
```javascript
async function pollJob(jobId) {
    while (true) {
        const job = await fetch(`/api/jobs/${jobId}`).then(r => r.json());
        updateJobUI(job);
        if (job.state !== 'running') break;
        await sleep(2000);
    }
}
```

**Log Streaming:**
- Logs stored in `data/logs/episodes/{episode_id}.log`
- API endpoint `/api/jobs/<job_id>` returns last 500 lines
- Frontend displays logs in scrollable `<pre>` element
- Auto-scrolls to bottom on new lines

### Multi-Channel Support

**Channel Selector:**
- Dropdown in UI: "All Channels" or specific channel
- Filters episode list by `channel_id`
- Batch processing can target specific channel

**Channel Management:**
- Channels stored in `channels` table
- Web UI reads from database (no hardcoding)
- Future: Add/edit channels via admin UI

---

## 8. CLI Commands

### Automation Commands (Cron-Ready)

These commands are designed for automated execution (cron, systemd timers) with clean exit codes.

| Command | Description | Exit Code |
|---------|-------------|-----------|
| `btcedu run-latest` | Detect + process newest pending episode | 0 = success, 1 = failure |
| `btcedu run-pending --max N` | Process N pending episodes | 0 = success, 1 = failure |
| `btcedu run-pending --since YYYY-MM-DD` | Process episodes since date | 0 = success, 1 = failure |
| `btcedu retry --episode-id ID` | Retry failed episode from last stage | 0 = success, 1 = failure |

**Logging:** All output goes to stdout/stderr, suitable for systemd journal.

### Manual Stage Commands

| Command | Description | Options |
|---------|-------------|---------|
| `btcedu detect` | Check RSS feed for new episodes | None |
| `btcedu backfill` | Import full channel history via yt-dlp | `--since`, `--until`, `--max`, `--dry-run` |
| `btcedu download --episode-id ID` | Download audio | `--force` |
| `btcedu transcribe --episode-id ID` | Transcribe via Whisper | `--force` |
| `btcedu chunk --episode-id ID` | Chunk transcript + FTS5 index | `--force` |
| `btcedu generate --episode-id ID` | Generate content | `--force`, `--top-k N` |
| `btcedu run --episode-id ID` | Run full pipeline | `--force` |

**Force Mode:** `--force` re-runs stage even if output exists (bypasses idempotency).

### Monitoring Commands

| Command | Description | Output |
|---------|-------------|--------|
| `btcedu status` | Episode counts by status + last 10 episodes | Table |
| `btcedu cost [--episode-id ID]` | API usage costs breakdown | JSON |
| `btcedu report --episode-id ID` | Show latest pipeline report | JSON |
| `btcedu journal [--tail N]` | Show project progress log | Markdown |
| `btcedu llm-report [--json-only] [--output FILE]` | Generate LLM usage introspection report | JSON/Markdown |

### Database Management

| Command | Description | Options |
|---------|-------------|---------|
| `btcedu init-db` | Initialize database (create tables + FTS5) | None |
| `btcedu migrate` | Apply pending database migrations | `--dry-run` |
| `btcedu migrate-status` | Show applied and pending migrations | None |

**Migration Safety:** Always run `btcedu migrate-status` before updating to check for pending migrations.

### Web Dashboard

| Command | Description | Options |
|---------|-------------|---------|
| `btcedu web` | Start Flask development server | `--host`, `--port` |
| `btcedu web --host 0.0.0.0 --port 5000` | Bind to LAN (accessible from other devices) | |

**Production:** Use `gunicorn` instead (see deployment section).

---

## 9. Deployment Architecture

### Production Environment (Raspberry Pi)

btcedu is deployed on a Raspberry Pi running Ubuntu with:
- **Application:** btcedu (Python 3.12+ virtual environment)
- **Web Server:** Gunicorn (WSGI server, localhost:8091)
- **Reverse Proxy:** Caddy (HTTPS + basic auth)
- **DNS:** DuckDNS (dynamic DNS for home network)
- **Automation:** systemd timers (detect every 6h, run daily)

### Architecture Diagram

```
Internet
   │
   ▼
DuckDNS (lnodebtc.duckdns.org)
   │
   ▼
Router (port forward 443 → 443)
   │
   ▼
Caddy (:443)
   ├─ HTTPS (Let's Encrypt auto-renewal)
   ├─ Basic Auth
   └─ Reverse Proxy
         │
         ▼
   Gunicorn (:8091, localhost only)
         │
         ▼
   Flask App (btcedu.web)
         │
         ▼
   SQLite Database + File System
```

### systemd Services

**Detection Timer:** `btcedu-detect.timer` / `btcedu-detect.service`
- **Schedule:** Every 6 hours
- **Command:** `btcedu detect`
- **Purpose:** Check RSS feed for new episodes

**Processing Timer:** `btcedu-run.timer` / `btcedu-run.service`
- **Schedule:** Daily at 02:00
- **Command:** `btcedu run-pending --max 3`
- **Purpose:** Process up to 3 pending episodes daily

**Web Dashboard:** `btcedu-web.service`
- **Type:** Long-running service
- **Command:** `gunicorn -b 127.0.0.1:8091 -w 1 'btcedu.web.app:create_app_from_env()'`
- **Purpose:** Serve web dashboard (localhost only)

### Caddy Reverse Proxy

**Caddyfile Snippet:**
```
lnodebtc.duckdns.org {
    @dashboard path /dashboard/*
    handle @dashboard {
        uri strip_prefix /dashboard
        basicauth {
            pi $2a$14$... # bcrypt hash of password
        }
        reverse_proxy 127.0.0.1:8091
    }
    header {
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy no-referrer
    }
}
```

**Features:**
- **HTTPS:** Automatic Let's Encrypt certificates with auto-renewal
- **Basic Auth:** Password-protected dashboard (bcrypt hashes)
- **Security Headers:** Prevents XSS, clickjacking, MIME sniffing
- **Path Prefix:** Dashboard at `/dashboard/` (allows other services on same domain)

### Environment Variables (.env)

All secrets stored in `.env` file:

```bash
# API Keys (required)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Podcast Source (required)
PODCAST_YOUTUBE_CHANNEL_ID=UCxxxxx
SOURCE_TYPE=youtube_rss

# Database (optional, defaults to data/btcedu.db)
DATABASE_URL=sqlite:///data/btcedu.db

# Data Directories (optional)
RAW_DATA_DIR=data/raw
TRANSCRIPTS_DIR=data/transcripts
CHUNKS_DIR=data/chunks
OUTPUTS_DIR=data/outputs
REPORTS_DIR=data/reports
LOGS_DIR=data/logs

# Processing Parameters (optional)
CHUNK_SIZE=1500
CHUNK_OVERLAP=0.15
CLAUDE_MODEL=claude-sonnet-4-20250514
CLAUDE_MAX_TOKENS=4096
CLAUDE_TEMPERATURE=0.3
DRY_RUN=false
```

**Security:**
- `.env` file excluded from git (`.gitignore`)
- systemd services load `.env` via `EnvironmentFile=`
- No secrets in code or systemd unit files

### GitHub Actions CI/CD

**.github/workflows/ci.yml**
- Runs on every push + PR
- Steps:
  1. Checkout code
  2. Setup Python 3.12
  3. Install dependencies
  4. Run ruff linter
  5. Run pytest with dummy API keys

**.github/workflows/deploy.yml**
- Runs on push to `main` + manual trigger
- Steps:
  1. Setup SSH with deploy key
  2. SSH to Raspberry Pi and run deploy script (`run.sh`)

**.github/workflows/security.yml**
- Runs weekly + on push
- Steps:
  1. Dependency vulnerability scan
  2. Secret detection

**Manual Deployment:** SSH to Raspberry Pi, then:
```bash
cd /home/pi/AI-Startup-Lab/bitcoin-education
git pull
source .venv/bin/activate
pip install -e ".[web]"
btcedu migrate
sudo systemctl restart btcedu-web
```

### Monitoring & Logs

**systemd Journal:**
```bash
# View detection logs
journalctl -u btcedu-detect --since today

# View processing logs
journalctl -u btcedu-run --since today

# View web dashboard logs
journalctl -u btcedu-web -f
```

**Application Logs:**
- `data/logs/web.log` - HTTP access logs
- `data/logs/web_errors.log` - Unhandled exceptions
- `data/logs/episodes/{episode_id}.log` - Per-episode job logs

**Health Check:**
```bash
curl -u pi:PASSWORD https://lnodebtc.duckdns.org/dashboard/api/health
```

---

## 10. How to Extend the System

### Add a New Pipeline Stage

**Example:** Add a "thumbnail generation" stage after "generate".

**Steps:**

1. **Define Stage Enum** (`btcedu/models/episode.py`):
   ```python
   class PipelineStage(str, enum.Enum):
       # ... existing stages ...
       GENERATE_THUMBNAIL = "generate_thumbnail"
   ```

2. **Update Status Enum** (if needed):
   ```python
   class EpisodeStatus(str, enum.Enum):
       # ... existing statuses ...
       THUMBNAIL_GENERATED = "thumbnail_generated"
   ```

3. **Create Stage Module** (`btcedu/core/thumbnail_generator.py`):
   ```python
   def generate_thumbnail(session, settings, episode_id: str):
       # Load episode
       # Call DALL-E API or generate locally
       # Save to data/outputs/{episode_id}/thumbnail.png
       # Update episode status
       # Record PipelineRun
   ```

4. **Integrate into Pipeline** (`btcedu/core/pipeline.py`):
   ```python
   def run_pipeline(session, settings, episode_id: str):
       # ... existing stages ...
       if episode.status < EpisodeStatus.THUMBNAIL_GENERATED:
           generate_thumbnail(session, settings, episode_id)
   ```

5. **Add CLI Command** (`btcedu/cli.py`):
   ```python
   @cli.command()
   @click.option("--episode-id", required=True)
   def generate_thumbnail(ctx, episode_id: str):
       # Call stage function
   ```

6. **Add Web API Endpoint** (`btcedu/web/api.py`):
   ```python
   @api.route("/episodes/<episode_id>/generate-thumbnail", methods=["POST"])
   def generate_thumbnail_endpoint(episode_id):
       # Queue job via JobManager
   ```

### Add a New Content Artifact

**Example:** Add a "podcast intro script" artifact.

**Steps:**

1. **Create Prompt Template** (`btcedu/prompts/intro.py`):
   ```python
   def build_intro_prompt(episode_info, chunks):
       return f"""
       Create a 30-second podcast intro script in Turkish...
       Episode: {episode_info.title}
       Relevant chunks: {chunks}
       """
   ```

2. **Update Generator** (`btcedu/core/generator.py`):
   ```python
   ARTIFACT_TYPES = [
       "outline", "script", "shorts", "visuals", "qa", "publishing",
       "intro"  # Add new type
   ]

   def generate_content(session, settings, episode_id: str):
       # ... existing artifacts ...
       # Add intro generation
       intro_prompt = build_intro_prompt(episode_info, chunks)
       intro_text = call_claude(intro_prompt)
       save_artifact(episode_id, "intro.tr.txt", intro_text)
   ```

3. **Update ContentArtifact Model** (optional, if tracking separately):
   ```python
   # Already supports arbitrary artifact_type strings
   ```

4. **Update Web Viewer** (`btcedu/web/templates/index.html` + `static/app.js`):
   ```javascript
   // Add new tab for intro script
   ```

### Add a New REST API Endpoint

**Example:** Add an endpoint to export episode data as CSV.

**Steps:**

1. **Add Route** (`btcedu/web/api.py`):
   ```python
   @api.route("/export/episodes.csv", methods=["GET"])
   def export_episodes_csv():
       session = get_session()
       episodes = session.query(Episode).all()

       output = io.StringIO()
       writer = csv.writer(output)
       writer.writerow(["Episode ID", "Title", "Status", "Cost"])
       for ep in episodes:
           writer.writerow([ep.episode_id, ep.title, ep.status, ...])

       return Response(output.getvalue(), mimetype="text/csv")
   ```

2. **Add Frontend Button** (`btcedu/web/templates/index.html`):
   ```html
   <button onclick="window.location.href='/api/export/episodes.csv'">
       Export CSV
   </button>
   ```

### Add a New UI Feature

**Example:** Add a "retry all failed episodes" button.

**Steps:**

1. **Add Backend Endpoint** (`btcedu/web/api.py`):
   ```python
   @api.route("/batch/retry-failed", methods=["POST"])
   def retry_failed_batch():
       session = get_session()
       failed = session.query(Episode).filter(
           Episode.status == EpisodeStatus.FAILED
       ).all()

       batch_id = str(uuid.uuid4())
       batch_job = BatchJob(batch_id=batch_id, episode_ids=[e.episode_id for e in failed])
       job_manager.start_batch(batch_job)

       return jsonify({"batch_id": batch_id})
   ```

2. **Add Frontend Button** (`btcedu/web/static/app.js`):
   ```javascript
   async function retryAllFailed() {
       const response = await fetch("/api/batch/retry-failed", {method: "POST"});
       const data = await response.json();
       pollBatch(data.batch_id);
   }
   ```

3. **Add UI Element** (`btcedu/web/templates/index.html`):
   ```html
   <button onclick="retryAllFailed()">Retry All Failed</button>
   ```

### Add a New Data Source (e.g., Podcast RSS)

**Example:** Add support for non-YouTube RSS feeds.

**Steps:**

1. **Update FeedService** (`btcedu/services/feed_service.py`):
   ```python
   def fetch_rss_feed(rss_url: str) -> list[EpisodeInfo]:
       feed = feedparser.parse(rss_url)
       episodes = []
       for entry in feed.entries:
           episodes.append(EpisodeInfo(
               episode_id=hashlib.md5(entry.link.encode()).hexdigest()[:16],
               title=entry.title,
               url=entry.link,
               published_at=entry.published_parsed,
               source="rss"
           ))
       return episodes
   ```

2. **Update Detector** (`btcedu/core/detector.py`):
   ```python
   def detect_episodes(session, settings):
       if settings.source_type == "youtube_rss":
           episodes = fetch_youtube_rss(settings.channel_id)
       elif settings.source_type == "rss":
           episodes = fetch_rss_feed(settings.rss_url)
       # ... rest of logic
   ```

3. **Update Config** (`.env.example`):
   ```bash
   SOURCE_TYPE=rss  # or youtube_rss
   PODCAST_RSS_URL=https://example.com/feed.rss
   ```

4. **Update Channel Model** (already supports `rss_url` field)

### Add a New LLM Provider

**Example:** Add support for OpenAI GPT-4 as an alternative to Claude.

**Steps:**

1. **Create Service Wrapper** (`btcedu/services/openai_service.py`):
   ```python
   def call_gpt4(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
       client = OpenAI(api_key=settings.openai_api_key)
       response = client.chat.completions.create(
           model="gpt-4-turbo",
           messages=[
               {"role": "system", "content": system_prompt},
               {"role": "user", "content": user_prompt}
           ]
       )
       return (
           response.choices[0].message.content,
           response.usage.prompt_tokens,
           response.usage.completion_tokens
       )
   ```

2. **Update Generator** (`btcedu/core/generator.py`):
   ```python
   def generate_content(session, settings, episode_id: str):
       if settings.llm_provider == "claude":
           response = claude_service.call_claude(system, user)
       elif settings.llm_provider == "openai":
           response = openai_service.call_gpt4(system, user)
       # ... rest of logic
   ```

3. **Update Config** (`.env.example`):
   ```bash
   LLM_PROVIDER=claude  # or openai
   ```

---

## 11. Development Workflow

### Running Tests

```bash
# Activate virtual environment
source .venv/bin/activate

# Run all tests
ANTHROPIC_API_KEY=dummy OPENAI_API_KEY=dummy DRY_RUN=true pytest tests/ -v

# Run specific test file
pytest tests/test_chunker.py -v

# Run with coverage
pytest tests/ --cov=btcedu --cov-report=html
```

**Note:** Tests run with `DRY_RUN=true` to avoid actual API calls.

### Linting & Formatting

```bash
# Check linting
ruff check btcedu/ tests/

# Auto-fix linting issues
ruff check btcedu/ tests/ --fix

# Check formatting
ruff format --check btcedu/ tests/

# Format code
ruff format btcedu/ tests/
```

### Pre-commit Hooks (Optional)

```bash
pip install pre-commit
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

### CI Pipeline

**GitHub Actions** run on every push:
1. **Linting:** `ruff check`
2. **Formatting:** `ruff format --check`
3. **Tests:** `pytest tests/`

All checks must pass before merging PRs.

### Database Migrations

**Creating a Migration:**

1. **Create Migration File** (`btcedu/migrations/002_new_migration.py`):
   ```python
   version = "002_new_migration"

   def upgrade(session):
       """Apply migration."""
       session.execute(text("ALTER TABLE episodes ADD COLUMN new_field TEXT"))

   def downgrade(session):
       """Rollback migration."""
       session.execute(text("ALTER TABLE episodes DROP COLUMN new_field"))
   ```

2. **Register Migration** (`btcedu/migrations/__init__.py`):
   ```python
   from btcedu.migrations import migration_002_new_migration

   MIGRATIONS = [
       migration_001_add_channels_support,
       migration_002_new_migration,  # Add here
   ]
   ```

3. **Test Migration:**
   ```bash
   btcedu migrate --dry-run  # Preview SQL
   btcedu migrate            # Apply migration
   btcedu migrate-status     # Verify
   ```

**Migration Best Practices:**
- Always test with `--dry-run` first
- Migrations must be idempotent (safe to re-run)
- Never modify existing migrations (create new ones)
- Always update `MIGRATIONS` list in order

### Progress Logging (Journal)

**btcedu uses an append-only development journal** (`docs/PROGRESS_LOG.md`) to track:
- Feature plans and designs
- Implementation milestones
- Decisions and rationale
- How to resume work in a new session

**Adding to Journal:**

```bash
# CLI
btcedu journal  # View full log
btcedu journal --tail 30  # Last 30 lines

# Manual (append to docs/PROGRESS_LOG.md)
echo "## 2026-02-14 - Added thumbnail generation" >> docs/PROGRESS_LOG.md
```

**Using Journal to Resume Work:**
- Read last 2-3 entries to understand recent context
- Check "Next steps" sections
- Verify no pending migrations or broken tests

### Local Development Server

```bash
# Start web dashboard in debug mode
btcedu web

# Or with gunicorn (production-like)
gunicorn -b 127.0.0.1:8091 --reload 'btcedu.web.app:create_app_from_env()'
```

**Hot Reloading:**
- Flask debug mode: Auto-reloads on file changes
- Gunicorn `--reload`: Auto-reloads on file changes (slower)

### Debugging Tips

**Enable Debug Logging:**
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

**Inspect Database:**
```bash
sqlite3 data/btcedu.db
.tables
.schema episodes
SELECT * FROM episodes WHERE status = 'failed';
```

**Test FTS5 Search:**
```bash
sqlite3 data/btcedu.db
SELECT chunk_id, snippet(chunks_fts, 0, '[', ']', '...', 10)
FROM chunks_fts
WHERE chunks_fts MATCH 'bitcoin'
LIMIT 5;
```

**Dry-Run Mode:**
```bash
DRY_RUN=true btcedu generate --episode-id <ID>
# Writes prompts to data/outputs/{episode_id}/prompts/ without calling API
```

---

## Appendix: Technology Choices

### Why SQLite?

- **Zero configuration:** No separate database server
- **FTS5 built-in:** Fast full-text search without external dependencies
- **Single-writer workload:** btcedu processes one episode at a time
- **Portable:** Entire database in a single file
- **Reliable:** ACID compliance, crash recovery

**Trade-off:** Not suitable for high-concurrency writes (but we don't need that).

### Why Flask?

- **Lightweight:** Minimal overhead for small dashboard
- **Flexible:** Easy to add custom endpoints
- **Mature:** Stable, well-documented, large ecosystem
- **Python-native:** Same language as core pipeline

**Trade-off:** Not as fast as FastAPI, but performance is not a bottleneck for this use case.

### Why Vanilla JavaScript?

- **No build step:** Faster development, simpler deployment
- **Small codebase:** ~500 lines of JS, doesn't justify React/Vue overhead
- **Fast load times:** No bundle, no dependencies
- **Easy debugging:** Readable code in browser DevTools

**Trade-off:** No component framework, but UI is simple enough not to need one.

### Why Claude over GPT-4?

- **Citation quality:** Better at following citation format instructions
- **Turkish quality:** Comparable or better Turkish output
- **API ergonomics:** Simpler prompt structure (system + user)
- **Cost:** Sonnet pricing competitive with GPT-4o

**Trade-off:** Could add GPT-4 support as alternative (see extension guide).

### Why FTS5 over Vector Embeddings?

- **Simplicity:** No embedding API calls, no vector DB
- **Speed:** Fast enough for <1000 chunks per episode
- **Accuracy:** Keyword search works well for citation-based retrieval
- **Cost:** Zero API cost for retrieval

**Trade-off:** Semantic search (embeddings) might be better for abstract queries, but not needed for this use case.

---

## Glossary

| Term | Definition |
|------|------------|
| **Artifact** | A generated content file (outline, script, etc.) |
| **Chunk** | A segment of transcript text (1500 chars) used for RAG |
| **FTS5** | SQLite's full-text search extension |
| **Idempotent** | Safe to run multiple times without side effects |
| **Pipeline** | The sequence of stages (download → transcribe → chunk → generate) |
| **RAG** | Retrieval-Augmented Generation (Claude + chunk search) |
| **Whisper** | OpenAI's speech-to-text API |
| **yt-dlp** | Command-line tool for downloading YouTube videos |

---

**End of Architecture Documentation**

For quickstart instructions, see [README.md](../README.md).
For development history, see [docs/PROGRESS_LOG.md](PROGRESS_LOG.md).
For questions or contributions, open an issue on GitHub.
