import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from btcedu.config import get_settings
from btcedu.db import get_session_factory, init_db
from btcedu.models.episode import Episode, EpisodeStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """btcedu - Bitcoin Education Automation Pipeline"""
    ctx.ensure_object(dict)

    # Don't initialize database during resilient parsing (help, completion)
    if ctx.resilient_parsing:
        return

    try:
        # Allow tests to inject settings and session_factory via ctx.obj
        if "settings" not in ctx.obj:
            settings = get_settings()
            ctx.obj["settings"] = settings
        else:
            settings = ctx.obj["settings"]

        if "session_factory" not in ctx.obj:
            init_db(settings.database_url)
            ctx.obj["session_factory"] = get_session_factory(settings.database_url)

            # Check for pending migrations on CLI startup
            _check_pending_migrations(ctx.obj["session_factory"])
    except Exception as e:
        # If database initialization fails during help display, store the error
        # The actual command will fail properly if it tries to access the database
        # This allows --help to work even when the database is unavailable
        import sqlalchemy.exc

        if isinstance(e, (sqlalchemy.exc.OperationalError, sqlalchemy.exc.DatabaseError)):
            ctx.obj["db_init_error"] = e
        else:
            # Re-raise non-database errors
            raise


def _check_pending_migrations(session_factory):
    """Check and warn if there are pending migrations."""
    session = session_factory()
    try:
        from btcedu.migrations import get_pending_migrations

        pending = get_pending_migrations(session)
        if pending:
            migration_list = ", ".join(m.version for m in pending)
            logging.warning(f"⚠ Database migration required. Pending: {migration_list}")
            logging.warning("  Run: btcedu migrate")
    except Exception:
        # Silently ignore errors during migration check
        # (e.g., if schema_migrations table doesn't exist yet)
        pass
    finally:
        session.close()


@cli.command()
@click.option(
    "--profile",
    default=None,
    help="Content profile to assign to newly detected episodes.",
)
@click.pass_context
def detect(ctx: click.Context, profile: str | None) -> None:
    """Check feed for new episodes and insert into DB."""
    from btcedu.core.detector import detect_episodes

    settings = ctx.obj["settings"]
    # If --profile specified, temporarily override default_content_profile
    if profile:
        settings = settings.model_copy(update={"default_content_profile": profile})
    session = ctx.obj["session_factory"]()
    try:
        result = detect_episodes(session, settings)
        click.echo(f"Found: {result.found}  New: {result.new}  Total in DB: {result.total}")
    finally:
        session.close()


@cli.command()
@click.option("--max", "max_count", type=int, default=None, help="Max new episodes to insert.")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only videos published on or after YYYY-MM-DD.",
)
@click.option(
    "--until",
    "until_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only videos published on or before YYYY-MM-DD.",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Print what would be inserted, don't write."
)
@click.pass_context
def backfill(
    ctx: click.Context,
    max_count: int | None,
    since: datetime | None,
    until_date: datetime | None,
    dry_run: bool,
) -> None:
    """Import full YouTube channel history via yt-dlp.

    Unlike 'detect' (which reads the RSS feed limited to ~15 videos),
    this command uses yt-dlp to list ALL videos from the channel.
    """
    from btcedu.core.detector import backfill_episodes

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        result = backfill_episodes(
            session,
            settings,
            max_count=max_count,
            since=since.date() if since else None,
            until=until_date.date() if until_date else None,
            dry_run=dry_run,
        )
        prefix = "[dry-run] " if dry_run else ""
        click.echo(f"{prefix}Found: {result.found}  New: {result.new}  Total in DB: {result.total}")
    except Exception as e:
        click.echo(f"Backfill failed: {e}", err=True)
        sys.exit(1)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to download (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-download even if file exists.")
@click.pass_context
def download(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Download audio for specified episodes."""
    from btcedu.core.detector import download_episode

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                path = download_episode(session, eid, settings, force=force)
                click.echo(f"[OK] {eid} -> {path}")
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to transcribe (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-transcribe even if file exists.")
@click.pass_context
def transcribe(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Transcribe audio for specified episodes via Whisper API."""
    from btcedu.core.transcriber import transcribe_episode

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                path = transcribe_episode(session, eid, settings, force=force)
                click.echo(f"[OK] {eid} -> {path}")
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to chunk (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-chunk even if file exists.")
@click.pass_context
def chunk(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Chunk transcripts for specified episodes."""
    from btcedu.core.transcriber import chunk_episode

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                count = chunk_episode(session, eid, settings, force=force)
                click.echo(f"[OK] {eid} -> {count} chunks")
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    help="Episode ID(s) to process (repeatable). If omitted, processes all pending episodes.",
)
@click.option("--force", is_flag=True, default=False, help="Force re-run of completed stages.")
@click.option(
    "--profile",
    default=None,
    help="Only process episodes with this content profile (when no --episode-id given).",
)
@click.pass_context
def run(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, profile: str | None) -> None:
    """Run the full pipeline for specific or all pending episodes."""
    from btcedu.core.pipeline import run_episode_pipeline, write_report

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        if episode_ids:
            episodes = session.query(Episode).filter(Episode.episode_id.in_(episode_ids)).all()
        else:
            q = (
                session.query(Episode)
                .filter(
                    Episode.status.in_(
                        [
                            EpisodeStatus.NEW,
                            EpisodeStatus.DOWNLOADED,
                            EpisodeStatus.TRANSCRIBED,
                            EpisodeStatus.CHUNKED,
                            EpisodeStatus.GENERATED,
                        ]
                    )
                )
                .order_by(Episode.published_at.asc())
            )
            if profile:
                q = q.filter(Episode.content_profile == profile)
            episodes = q.all()

        if not episodes:
            click.echo("No episodes to process.")
            return

        has_failure = False
        for ep in episodes:
            click.echo(f"Processing: {ep.episode_id} ({ep.title})")
            report = run_episode_pipeline(session, ep, settings, force=force)
            write_report(report, settings.reports_dir)

            for sr in report.stages:
                if sr.status == "success":
                    click.echo(f"  {sr.stage}: {sr.detail} ({sr.duration_seconds:.1f}s)")
                elif sr.status == "failed":
                    click.echo(f"  {sr.stage}: FAILED - {sr.error}", err=True)

            if report.success:
                click.echo(f"  -> OK (${report.total_cost_usd:.4f})")
            else:
                click.echo(f"  -> FAILED: {report.error}", err=True)
                has_failure = True

        if has_failure:
            sys.exit(1)
    finally:
        session.close()


@cli.command(name="run-latest")
@click.option(
    "--profile",
    default=None,
    help="Only process episodes with this content profile.",
)
@click.pass_context
def run_latest_cmd(ctx: click.Context, profile: str | None) -> None:
    """Detect new episodes, then process the newest pending one."""
    from btcedu.core.pipeline import run_latest, write_report

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        report = run_latest(session, settings, profile=profile)

        if report is None:
            click.echo("No pending episodes to process.")
            return

        write_report(report, settings.reports_dir)

        click.echo(f"Episode: {report.episode_id} ({report.title})")
        for sr in report.stages:
            if sr.status == "success":
                click.echo(f"  {sr.stage}: {sr.detail} ({sr.duration_seconds:.1f}s)")
            elif sr.status == "failed":
                click.echo(f"  {sr.stage}: FAILED - {sr.error}", err=True)

        if report.success:
            click.echo(f"-> OK (${report.total_cost_usd:.4f})")
        else:
            click.echo(f"-> FAILED: {report.error}", err=True)
            sys.exit(1)
    finally:
        session.close()


@cli.command(name="run-pending")
@click.option("--max", "max_episodes", type=int, default=None, help="Max episodes to process.")
@click.option(
    "--since",
    type=click.DateTime(),
    default=None,
    help="Only episodes published after this date (YYYY-MM-DD).",
)
@click.option(
    "--profile",
    default=None,
    help="Only process episodes with this content profile.",
)
@click.pass_context
def run_pending_cmd(
    ctx: click.Context,
    max_episodes: int | None,
    since: datetime | None,
    profile: str | None,
) -> None:
    """Process all pending episodes through the pipeline."""
    from btcedu.core.pipeline import run_pending, write_report

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        # Add timezone info if since was provided
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        reports = run_pending(
            session, settings, max_episodes=max_episodes, since=since, profile=profile
        )

        if not reports:
            click.echo("No pending episodes to process.")
            return

        has_failure = False
        for report in reports:
            write_report(report, settings.reports_dir)
            status_str = "OK" if report.success else "FAILED"
            click.echo(
                f"  [{status_str}] {report.episode_id}: {report.title[:50]} "
                f"(${report.total_cost_usd:.4f})"
            )
            if not report.success:
                has_failure = True

        ok = sum(1 for r in reports if r.success)
        fail = len(reports) - ok
        total_cost = sum(r.total_cost_usd for r in reports)
        click.echo(f"\nDone: {ok} ok, {fail} failed, ${total_cost:.4f} total cost")

        if has_failure:
            sys.exit(1)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to retry (repeatable).",
)
@click.pass_context
def retry(ctx: click.Context, episode_ids: tuple[str, ...]) -> None:
    """Retry failed episodes from their last successful stage."""
    from btcedu.core.pipeline import retry_episode, write_report

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        has_failure = False
        for eid in episode_ids:
            try:
                report = retry_episode(session, eid, settings)
                write_report(report, settings.reports_dir)

                if report.success:
                    click.echo(f"[OK] {eid}: retry succeeded (${report.total_cost_usd:.4f})")
                else:
                    click.echo(f"[FAIL] {eid}: {report.error}", err=True)
                    has_failure = True
            except ValueError as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
                has_failure = True

        if has_failure:
            sys.exit(1)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id", "episode_id", type=str, required=True, help="Episode to show report for."
)
@click.pass_context
def report(ctx: click.Context, episode_id: str) -> None:
    """Show the latest pipeline report for an episode."""
    settings = ctx.obj["settings"]
    report_dir = Path(settings.reports_dir) / episode_id

    if not report_dir.exists():
        click.echo(f"No reports found for {episode_id}")
        return

    reports = sorted(report_dir.glob("report_*.json"), reverse=True)
    if not reports:
        click.echo(f"No reports found for {episode_id}")
        return

    latest = reports[0]
    data = json.loads(latest.read_text())

    click.echo(f"=== Report: {episode_id} ===")
    click.echo(f"  Title:     {data['title']}")
    click.echo(f"  Status:    {'OK' if data['success'] else 'FAILED'}")
    click.echo(f"  Started:   {data['started_at']}")
    click.echo(f"  Completed: {data['completed_at']}")
    click.echo(f"  Cost:      ${data['total_cost_usd']:.4f}")

    if data.get("error"):
        click.echo(f"  Error:     {data['error']}")

    click.echo("  Stages:")
    for stage in data.get("stages", []):
        status_str = stage["status"].upper()
        detail = stage.get("detail", "")
        error = stage.get("error", "")
        duration = stage.get("duration_seconds", 0)
        line = f"    {stage['stage']:<12} {status_str:<8} {duration:.1f}s"
        if detail:
            line += f"  {detail}"
        if error:
            line += f"  ERROR: {error}"
        click.echo(line)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show pipeline status: counts by status + last 10 episodes."""
    session = ctx.obj["session_factory"]()
    try:
        from sqlalchemy import func

        rows = session.query(Episode.status, func.count()).group_by(Episode.status).all()
        total = sum(c for _, c in rows)
        click.echo(f"=== Episodes: {total} ===")
        for s, c in rows:
            click.echo(f"  {s.value:<14} {c}")

        click.echo("")
        click.echo("--- Last 10 episodes ---")
        recent = session.query(Episode).order_by(Episode.detected_at.desc()).limit(10).all()
        if not recent:
            click.echo("  (none)")
        for ep in recent:
            pub = ep.published_at.strftime("%Y-%m-%d") if ep.published_at else "???"
            err = ""
            if ep.error_message:
                err = f"  !! {ep.error_message[:40]}"
            click.echo(f"  [{ep.status.value:<12}] {ep.episode_id}  {pub}  {ep.title[:50]}{err}")
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to generate content for (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Regenerate even if outputs exist.")
@click.option("--top-k", type=int, default=16, help="Number of chunks to retrieve for context.")
@click.pass_context
def generate(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, top_k: int) -> None:
    """Generate Turkish content package for CHUNKED episodes."""
    from btcedu.core.generator import generate_content

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = generate_content(session, eid, settings, force=force, top_k=top_k)
                click.echo(
                    f"[OK] {eid} -> {len(result.artifacts)} artifacts "
                    f"(${result.total_cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to refine (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-refine even if v2 outputs exist.")
@click.pass_context
def refine(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Refine generated content using QA feedback (v1 -> v2)."""
    from btcedu.core.generator import refine_content

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = refine_content(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {len(result.artifacts)} artifacts "
                    f"(${result.total_cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to correct (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-correct even if output exists.")
@click.pass_context
def correct(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Correct Whisper transcripts for specified episodes (v2 pipeline)."""
    from btcedu.core.corrector import correct_transcript

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = correct_transcript(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {result.corrected_path} "
                    f"({result.change_count} changes, ${result.cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to segment into stories (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-segment even if output exists.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON instead of calling Claude API.",
)
@click.pass_context
def segment(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Segment corrected broadcast transcript into discrete news stories (v2 news pipeline)."""
    from btcedu.core.segmenter import segment_broadcast

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = segment_broadcast(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> segment not enabled or already up-to-date")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.story_count} stories, "
                        f"~{result.total_duration_seconds}s total, "
                        f"${result.cost_usd:.4f}"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to translate (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-translate even if output exists.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON instead of calling Claude API.",
)
@click.pass_context
def translate(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Translate corrected German transcripts to Turkish (v2 pipeline)."""
    from btcedu.core.translator import translate_transcript

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = translate_transcript(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.translated_path} "
                        f"({result.input_char_count}→{result.output_char_count} chars, "
                        f"${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to adapt (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-adapt even if output exists.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON instead of calling Claude API.",
)
@click.pass_context
def adapt(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Adapt Turkish translation for Turkey context (v2 pipeline)."""
    from btcedu.core.adapter import adapt_script

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = adapt_script(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.adapted_path} "
                        f"({result.adaptation_count} adaptations: "
                        f"T1={result.tier1_count}, T2={result.tier2_count}, "
                        f"${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to chapterize (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-chapterize even if output exists.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON instead of calling Claude API.",
)
@click.pass_context
def chapterize(
    ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool
) -> None:
    """Chapterize adapted script into production JSON (v2 pipeline)."""
    from btcedu.core.chapterizer import chapterize_script

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = chapterize_script(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.chapter_count} chapters, "
                        f"~{result.estimated_duration_seconds}s total, "
                        f"{result.input_tokens} in / {result.output_tokens} out "
                        f"(${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to generate images for (repeatable).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Regenerate all images even if they exist.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON instead of calling APIs.",
)
@click.option(
    "--chapter",
    "chapter_id",
    default=None,
    help="Regenerate images for a specific chapter only.",
)
@click.pass_context
def imagegen(
    ctx: click.Context,
    episode_ids: tuple[str, ...],
    force: bool,
    dry_run: bool,
    chapter_id: str | None,
) -> None:
    """Generate images for chapters (v2 pipeline, Sprint 7)."""
    from btcedu.core.image_generator import generate_images

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = generate_images(session, eid, settings, force=force, chapter_id=chapter_id)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.generated_count}/{result.image_count} "
                        f"images generated, {result.template_count} placeholders, "
                        f"{result.failed_count} failed, {result.input_tokens} in / "
                        f"{result.output_tokens} out (${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to generate TTS audio for (repeatable).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Regenerate all audio even if current.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write silent MP3 placeholders instead of calling ElevenLabs API.",
)
@click.option(
    "--chapter",
    "chapter_id",
    default=None,
    help="Regenerate audio for a specific chapter only.",
)
@click.pass_context
def tts(
    ctx: click.Context,
    episode_ids: tuple[str, ...],
    force: bool,
    dry_run: bool,
    chapter_id: str | None,
) -> None:
    """Generate TTS audio for chapters (v2 pipeline, Sprint 8)."""
    from btcedu.core.tts import generate_tts

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = generate_tts(session, eid, settings, force=force, chapter_id=chapter_id)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.segment_count} segments, "
                        f"{result.total_duration_seconds:.1f}s total, "
                        f"{result.total_characters} chars "
                        f"(${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to render video for (repeatable).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-render even if draft video is current.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Generate render manifest and segments without executing ffmpeg.",
)
@click.pass_context
def render(
    ctx: click.Context,
    episode_ids: tuple[str, ...],
    force: bool,
    dry_run: bool,
) -> None:
    """Render draft video from chapters, images, and TTS audio (v2 pipeline, Sprint 9)."""
    from btcedu.core.renderer import render_video

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = render_video(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.segment_count} segments, "
                        f"{result.total_duration_seconds:.1f}s, "
                        f"{result.total_size_bytes / 1024 / 1024:.1f}MB"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.group()
@click.pass_context
def review(ctx: click.Context) -> None:
    """Review system commands."""
    pass


@review.command(name="list")
@click.option(
    "--status",
    default=None,
    help="Filter by status (pending, approved, rejected, changes_requested).",
)
@click.pass_context
def review_list(ctx: click.Context, status: str | None) -> None:
    """List review tasks."""
    from btcedu.models.review import ReviewStatus, ReviewTask

    session = ctx.obj["session_factory"]()
    try:
        query = session.query(ReviewTask).order_by(ReviewTask.created_at.desc())

        if status:
            query = query.filter(ReviewTask.status == status)
        else:
            # Default: show pending + in_review
            query = query.filter(
                ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value])
            )

        tasks = query.all()
        if not tasks:
            click.echo("No review tasks found.")
            return

        click.echo(f"{'ID':<5} {'Episode':<20} {'Stage':<10} {'Status':<20} {'Created'}")
        click.echo("-" * 80)
        for t in tasks:
            ep = session.query(Episode).filter(Episode.episode_id == t.episode_id).first()
            title = ep.title[:18] if ep else t.episode_id[:18]
            created = t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "?"
            click.echo(f"{t.id:<5} {title:<20} {t.stage:<10} {t.status:<20} {created}")
    finally:
        session.close()


@review.command()
@click.argument("review_id", type=int)
@click.option("--notes", default=None, help="Optional approval notes.")
@click.pass_context
def approve(ctx: click.Context, review_id: int, notes: str | None) -> None:
    """Approve a review task."""
    from btcedu.core.reviewer import approve_review

    session = ctx.obj["session_factory"]()
    try:
        decision = approve_review(session, review_id, notes=notes)
        click.echo(f"[OK] Review {review_id} approved (decision {decision.id})")
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@review.command()
@click.argument("review_id", type=int)
@click.option("--notes", default=None, help="Optional rejection notes.")
@click.pass_context
def reject(ctx: click.Context, review_id: int, notes: str | None) -> None:
    """Reject a review task (reverts episode to TRANSCRIBED)."""
    from btcedu.core.reviewer import reject_review

    session = ctx.obj["session_factory"]()
    try:
        decision = reject_review(session, review_id, notes=notes)
        click.echo(f"[OK] Review {review_id} rejected (decision {decision.id})")
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@review.command(name="request-changes")
@click.argument("review_id", type=int)
@click.option("--notes", required=True, help="Feedback describing changes needed.")
@click.pass_context
def request_changes_cmd(ctx: click.Context, review_id: int, notes: str) -> None:
    """Request changes on a review task (reverts episode and marks artifacts stale)."""
    from btcedu.core.reviewer import request_changes

    session = ctx.obj["session_factory"]()
    try:
        decision = request_changes(session, review_id, notes=notes)
        click.echo(f"[OK] Changes requested on review {review_id} (decision {decision.id})")
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@cli.group()
@click.pass_context
def prompt(ctx: click.Context) -> None:
    """Prompt version management commands."""
    pass


@prompt.command(name="list")
@click.option("--name", default=None, help="Filter by prompt name.")
@click.pass_context
def prompt_list(ctx: click.Context, name: str | None) -> None:
    """List prompt versions."""
    from btcedu.core.prompt_registry import PromptRegistry
    from btcedu.models.prompt_version import PromptVersion

    session = ctx.obj["session_factory"]()
    try:
        if name:
            registry = PromptRegistry(session)
            versions = registry.get_history(name)
        else:
            versions = (
                session.query(PromptVersion)
                .order_by(PromptVersion.name, PromptVersion.version.desc())
                .all()
            )

        if not versions:
            click.echo("No prompt versions registered.")
            return

        click.echo(
            f"{'ID':<5} {'Name':<25} {'Ver':<5} {'Default':<9} {'Hash':<14} {'Model':<30} {'Created'}"
        )
        click.echo("-" * 110)
        for pv in versions:
            created = pv.created_at.strftime("%Y-%m-%d %H:%M") if pv.created_at else "?"
            default = "✓" if pv.is_default else ""
            hash_short = pv.content_hash[:12] if pv.content_hash else "?"
            click.echo(
                f"{pv.id:<5} {pv.name:<25} {pv.version:<5} {default:<9} {hash_short:<14} "
                f"{(pv.model or ''):<30} {created}"
            )
    finally:
        session.close()


@prompt.command()
@click.argument("version_id", type=int)
@click.pass_context
def promote(ctx: click.Context, version_id: int) -> None:
    """Promote a prompt version to default."""
    from btcedu.core.prompt_registry import PromptRegistry

    session = ctx.obj["session_factory"]()
    try:
        registry = PromptRegistry(session)
        registry.promote_to_default(version_id)
        click.echo(f"[OK] Prompt version {version_id} promoted to default.")
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@cli.command()
@click.option("--episode-id", "episode_id", type=str, default=None, help="Filter by episode ID")
@click.pass_context
def cost(ctx: click.Context, episode_id: str | None) -> None:
    """Show API usage costs from PipelineRun records."""
    from sqlalchemy import func

    from btcedu.models.episode import PipelineRun

    session = ctx.obj["session_factory"]()
    try:
        query = session.query(
            PipelineRun.stage,
            func.count().label("runs"),
            func.sum(PipelineRun.input_tokens).label("input_tokens"),
            func.sum(PipelineRun.output_tokens).label("output_tokens"),
            func.sum(PipelineRun.estimated_cost_usd).label("total_cost"),
        )

        if episode_id:
            ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
            if not ep:
                click.echo(f"Episode not found: {episode_id}")
                return
            query = query.filter(PipelineRun.episode_id == ep.id)

        rows = query.group_by(PipelineRun.stage).all()

        if not rows:
            click.echo("No pipeline runs recorded yet.")
            return

        click.echo("=== API Usage Costs ===")
        grand_total = 0.0
        for row in rows:
            cost_val = row.total_cost or 0.0
            grand_total += cost_val
            click.echo(
                f"  {row.stage.value:<12} "
                f"runs={row.runs}  "
                f"in={row.input_tokens or 0:>8}  "
                f"out={row.output_tokens or 0:>8}  "
                f"${cost_val:.4f}"
            )
        click.echo(f"  {'TOTAL':<12} ${grand_total:.4f}")

        # Episode count and per-episode average
        ep_count = session.query(func.count(func.distinct(PipelineRun.episode_id))).scalar()
        if ep_count and ep_count > 0:
            click.echo(f"\n  Episodes processed: {ep_count}")
            click.echo(f"  Avg cost/episode:   ${grand_total / ep_count:.4f}")
    finally:
        session.close()


@cli.command(name="init-db")
@click.pass_context
def init_db_cmd(ctx: click.Context) -> None:
    """Initialize the database (create tables)."""
    click.echo("Database initialized successfully.")


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be applied without making changes.",
)
@click.pass_context
def migrate(ctx: click.Context, dry_run: bool) -> None:
    """Run pending database migrations."""
    from btcedu.migrations import get_pending_migrations, run_migrations

    session = ctx.obj["session_factory"]()
    try:
        pending = get_pending_migrations(session)

        if not pending:
            click.echo("✓ Database is up to date. No migrations needed.")
            return

        click.echo(f"Found {len(pending)} pending migration(s):")
        for migration in pending:
            click.echo(f"  • {migration.version}: {migration.description}")

        if dry_run:
            click.echo("\n[DRY RUN] No changes were made.")
            return

        click.echo("\nApplying migrations...")
        run_migrations(session, dry_run=False)
        click.echo("\n✓ All migrations completed successfully!")
    except Exception as e:
        click.echo(f"\n✗ Migration failed: {e}", err=True)
        sys.exit(1)
    finally:
        session.close()


@cli.command(name="migrate-status")
@click.pass_context
def migrate_status(ctx: click.Context) -> None:
    """Show migration status (applied and pending)."""
    from btcedu.migrations import get_applied_migrations, get_pending_migrations

    session = ctx.obj["session_factory"]()
    try:
        applied = get_applied_migrations(session)
        pending = get_pending_migrations(session)

        if applied:
            click.echo(f"Applied migrations ({len(applied)}):")
            for version in applied:
                click.echo(f"  ✓ {version}")
        else:
            click.echo("No migrations applied yet.")

        if pending:
            click.echo(f"\nPending migrations ({len(pending)}):")
            for migration in pending:
                click.echo(f"  • {migration.version}: {migration.description}")
            click.echo("\nRun 'btcedu migrate' to apply pending migrations.")
        else:
            click.echo("\n✓ Database is up to date.")
    finally:
        session.close()


@cli.command()
@click.option("--tail", "tail_n", type=int, default=50, help="Show last N lines.")
def journal(tail_n: int) -> None:
    """Show the project progress log."""
    from btcedu.utils.journal import JOURNAL_PATH

    if not JOURNAL_PATH.exists():
        click.echo(f"No journal yet at {JOURNAL_PATH}")
        return

    lines = JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= tail_n:
        click.echo("\n".join(lines))
    else:
        click.echo(f"... ({len(lines) - tail_n} lines above) ...\n")
        click.echo("\n".join(lines[-tail_n:]))


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN).")
@click.option("--port", default=5000, type=int, help="Port number.")
@click.option("--production", is_flag=True, default=False, help="Print gunicorn command instead.")
def web(host: str, port: int, production: bool) -> None:
    """Start the web dashboard."""
    if production:
        venv = Path(sys.executable).parent
        cmd = (
            f'{venv / "gunicorn"} -w 2 -b {host}:{port} --timeout 300 "btcedu.web.app:create_app()"'
        )
        click.echo("Run this command for production:\n")
        click.echo(f"  {cmd}")
        return

    from btcedu.web.app import create_app

    app = create_app()
    click.echo(f"Starting dashboard on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


@cli.group()
@click.pass_context
def stock(ctx: click.Context) -> None:
    """Stock image management (Pexels)."""
    pass


@stock.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to search images for (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-search even if candidates exist.")
@click.option("--per-page", default=None, type=int, help="Override candidates per chapter.")
@click.pass_context
def search(
    ctx: click.Context, episode_ids: tuple[str, ...], force: bool, per_page: int | None
) -> None:
    """Search Pexels for candidate images per chapter."""
    from btcedu.core.stock_images import search_stock_images

    settings = ctx.obj["settings"]
    if per_page is not None:
        settings.pexels_results_per_chapter = per_page

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = search_stock_images(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {result.chapters_searched} chapters, "
                    f"{result.total_candidates} candidates "
                    f"({result.skipped_chapters} skipped)"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@stock.command(name="list")
@click.option("--episode-id", required=True, help="Episode ID to show status for.")
@click.pass_context
def stock_list(ctx: click.Context, episode_id: str) -> None:
    """Show stock image selection status per chapter."""
    import json as json_mod
    from pathlib import Path

    settings = ctx.obj["settings"]
    ep_dir = Path(settings.outputs_dir) / episode_id
    manifest_path = ep_dir / "images" / "candidates" / "candidates_manifest.json"

    if not manifest_path.exists():
        click.echo(f"No candidates manifest for {episode_id}. Run 'btcedu stock search' first.")
        return

    manifest = json_mod.loads(manifest_path.read_text(encoding="utf-8"))
    chapters = manifest.get("chapters", {})

    if not chapters:
        click.echo("No chapter candidates found.")
        return

    click.echo(f"{'Chapter':<8} {'Rank':<6} {'Status':<12} {'Details':<35} {'Query'}")
    click.echo("-" * 100)

    for ch_id in sorted(chapters.keys()):
        ch_data = chapters[ch_id]
        candidates = ch_data.get("candidates", [])
        query = ch_data.get("search_query", "")

        locked = [c for c in candidates if c.get("locked")]
        selected = [c for c in candidates if c.get("selected")]

        if locked:
            status = "[locked]"
            detail = f"pexels:{locked[0]['pexels_id']}"
            rank = str(locked[0].get("rank", "-"))
        elif selected:
            status = "[selected]"
            detail = f"pexels:{selected[0]['pexels_id']}"
            rank = str(selected[0].get("rank", "-"))
        elif candidates:
            status = "[pending]"
            detail = f"{len(candidates)} candidates"
            rank = "-"
        else:
            status = "[empty]"
            detail = "no candidates"
            rank = "-"

        click.echo(
            f"{ch_id:<8} {rank:<6} {status:<12} {detail:<35} {query[:35]}"
        )


@stock.command(name="select")
@click.option("--episode-id", required=True)
@click.option(
    "--chapter", "chapter_id", required=True,
    help="Chapter to select image for (e.g. ch03).",
)
@click.option(
    "--photo-id", "pexels_id", required=True, type=int,
    help="Pexels photo ID to select.",
)
@click.option(
    "--lock", is_flag=True, default=False,
    help="Lock selection (won't change on re-search).",
)
@click.pass_context
def stock_select(
    ctx: click.Context, episode_id: str, chapter_id: str,
    pexels_id: int, lock: bool,
) -> None:
    """Select a specific Pexels photo for a chapter."""
    from btcedu.core.stock_images import select_stock_image

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        select_stock_image(
            session, episode_id, chapter_id, pexels_id, settings, lock=lock
        )
        locked_msg = " (locked)" if lock else ""
        click.echo(
            f"[OK] Selected pexels:{pexels_id} for "
            f"{episode_id}/{chapter_id}{locked_msg}"
        )
    except Exception as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@stock.command(name="rank")
@click.option(
    "--episode-id", required=True,
    help="Episode ID to rank candidates for.",
)
@click.option("--force", is_flag=True, default=False, help="Re-rank even locked chapters.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Skip LLM calls.")
@click.pass_context
def stock_rank(
    ctx: click.Context, episode_id: str, force: bool, dry_run: bool
) -> None:
    """Rank stock image candidates using LLM."""
    from btcedu.core.stock_images import rank_candidates

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True
    session = ctx.obj["session_factory"]()
    try:
        result = rank_candidates(session, episode_id, settings, force=force)
        click.echo(
            f"[OK] {episode_id} -> {result.chapters_ranked} ranked, "
            f"{result.chapters_skipped} skipped, "
            f"${result.total_cost_usd:.4f}"
        )
    except Exception as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@stock.command(name="auto-select")
@click.option("--episode-id", required=True)
@click.pass_context
def stock_auto_select(ctx: click.Context, episode_id: str) -> None:
    """Auto-select first candidate per chapter and finalize manifest (dev/test only)."""
    from btcedu.core.stock_images import auto_select_best

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        result = auto_select_best(session, episode_id, settings)
        click.echo(
            f"[OK] {episode_id} -> {result.selected_count} selected, "
            f"{result.placeholder_count} placeholders"
        )
    except Exception as e:
        click.echo(f"[FAIL] {e}", err=True)
    finally:
        session.close()


@cli.group()
@click.pass_context
def profile(ctx: click.Context) -> None:
    """Content profile management."""
    pass


@profile.command(name="list")
@click.pass_context
def profile_list(ctx: click.Context) -> None:
    """List all available content profiles."""
    from btcedu.profiles import get_registry, reset_registry

    settings = ctx.obj["settings"]
    reset_registry()
    registry = get_registry(settings)
    profiles = registry.list_profiles()

    if not profiles:
        click.echo("No profiles found.")
        return

    for p in profiles:
        click.echo(f"  {p.name:24s} {p.display_name} ({p.source_language}→{p.target_language})")


@profile.command(name="show")
@click.argument("name")
@click.pass_context
def profile_show(ctx: click.Context, name: str) -> None:
    """Show details for a specific content profile."""
    from btcedu.profiles import ProfileNotFoundError, get_registry, reset_registry

    settings = ctx.obj["settings"]
    reset_registry()
    registry = get_registry(settings)

    try:
        p = registry.get(name)
    except ProfileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"Profile: {p.name}")
    click.echo(f"  Display Name:     {p.display_name}")
    click.echo(f"  Source Language:   {p.source_language}")
    click.echo(f"  Target Language:   {p.target_language}")
    click.echo(f"  Domain:           {p.domain}")
    click.echo(f"  Pipeline Version: {p.pipeline_version}")
    click.echo(f"  Stages Enabled:   {p.stages_enabled}")
    if p.stage_config:
        click.echo(f"  Stage Config:     {json.dumps(p.stage_config, indent=4)}")
    if p.youtube:
        click.echo(f"  YouTube:          {json.dumps(p.youtube, indent=4)}")
    if p.review_gates:
        click.echo(f"  Review Gates:     {json.dumps(p.review_gates, indent=4)}")
    if p.prompt_namespace:
        click.echo(f"  Prompt Namespace: {p.prompt_namespace}")


@cli.command(name="smoke-test-pipeline")
@click.option(
    "--profile",
    default=None,
    help="Profile to validate (default: all profiles).",
)
@click.pass_context
def smoke_test_pipeline(ctx: click.Context, profile: str | None) -> None:
    """Validate a content profile's pipeline configuration (no DB, no API calls).

    Checks: profile YAML parse, stage list, prompt templates, YouTube metadata,
    TTS config, stock image vocabulary. PASS/FAIL output.
    """
    from pathlib import Path as _Path

    from btcedu.core.pipeline import _get_stages
    from btcedu.core.prompt_registry import TEMPLATES_DIR
    from btcedu.profiles import ProfileNotFoundError, get_registry, reset_registry

    settings = ctx.obj["settings"]
    reset_registry()
    registry = get_registry(settings)

    all_profiles = registry.list_profiles()
    if not all_profiles:
        click.echo("[FAIL] No profiles found.", err=True)
        raise SystemExit(1)

    profiles_to_check = (
        [registry.get(profile)]
        if profile
        else all_profiles
    )

    all_pass = True

    for p in profiles_to_check:
        click.echo(f"\n=== Profile: {p.name} ({p.display_name}) ===")
        fails = []

        # 1. Stage list
        try:
            from btcedu.models.episode import Episode as _Ep

            dummy_ep = _Ep.__new__(_Ep)
            dummy_ep.pipeline_version = 2
            dummy_ep.content_profile = p.name
            stages = _get_stages(settings, dummy_ep)
            stage_names = [s[0] for s in stages]
            click.echo(f"  Stages ({len(stages)}): {' → '.join(stage_names)}")
        except Exception as e:
            fails.append(f"Stage list: {e}")

        # 2. Prompt templates
        prompt_ns = p.prompt_namespace
        templates_to_check = ["system.md", "correct_transcript.md", "translate.md"]
        if p.stage_config.get("segment", {}).get("enabled"):
            templates_to_check.append("segment_broadcast.md")
        templates_dir = _Path(TEMPLATES_DIR)
        for tmpl_name in templates_to_check:
            if prompt_ns:
                ns_path = templates_dir / prompt_ns / tmpl_name
                base_path = templates_dir / tmpl_name
                if ns_path.exists():
                    click.echo(f"  Template {tmpl_name}: OK (namespace override)")
                elif base_path.exists():
                    click.echo(f"  Template {tmpl_name}: OK (base fallback)")
                else:
                    fails.append(f"Template {tmpl_name}: not found in {prompt_ns}/ or base")
            else:
                base_path = templates_dir / tmpl_name
                if base_path.exists():
                    click.echo(f"  Template {tmpl_name}: OK")
                else:
                    click.echo(f"  Template {tmpl_name}: missing (non-critical)")

        # 3. YouTube metadata preview
        yt = p.youtube
        if yt:
            click.echo(f"  YouTube category: {yt.get('category_id', 'default')}")
            click.echo(f"  YouTube tags: {yt.get('tags', [])}")
            click.echo(f"  YouTube language: {yt.get('default_language', 'tr')}")
        else:
            click.echo("  YouTube: (no profile override — using settings defaults)")

        # 4. TTS config
        tts_cfg = p.stage_config.get("tts", {})
        if tts_cfg:
            voice = tts_cfg.get("voice_id") or "(settings default)"
            click.echo(
                f"  TTS: voice_id={voice}, stability={tts_cfg.get('stability', 'default')}"
            )
        else:
            click.echo("  TTS: (no profile override — using settings defaults)")

        # 5. Render accent color
        render_cfg = p.stage_config.get("render", {})
        accent = render_cfg.get("accent_color", "#F7931A (default)")
        click.echo(f"  Render accent color: {accent}")

        # 6. Domain tag for stock images
        click.echo(f"  Stock image domain tag: {p.domain or 'finance (default)'}")

        # Result
        if fails:
            all_pass = False
            click.echo(f"  [FAIL] Issues:")
            for f in fails:
                click.echo(f"    - {f}", err=True)
        else:
            click.echo(f"  [PASS]")

    click.echo("")
    if all_pass:
        click.echo("[PASS] All profile smoke tests passed.")
    else:
        click.echo("[FAIL] Some profile smoke tests failed.", err=True)
        raise SystemExit(1)


@cli.command(name="smoke-test-video")
@click.option(
    "--resolution",
    default=None,
    help="Override render resolution (default: from settings, e.g. 1920x1080).",
)
@click.option(
    "--keep",
    is_flag=True,
    default=False,
    help="Keep temporary files after the test (default: delete on success).",
)
@click.pass_context
def smoke_test_video(ctx: click.Context, resolution: str | None, keep: bool) -> None:
    """Smoke-test the video pipeline (normalize + segment) against the local ffmpeg build.

    Generates a synthetic test video and audio via ffmpeg lavfi filters,
    runs normalize_video_clip() and create_video_segment(), then validates
    the output. No external files or API keys required.

    Run this on the Raspberry Pi to confirm the ARM64 ffmpeg build supports
    the required filters (testsrc2, libx264, yuv420p, stream_loop).

    Exit code 0 = all steps passed. Exit code 1 = any step failed.
    """
    import shutil
    import sys
    import tempfile

    from btcedu.services.ffmpeg_service import (
        create_video_segment,
        generate_silent_audio,
        generate_test_video,
        get_ffmpeg_version,
        normalize_video_clip,
        probe_media,
    )

    settings = ctx.obj["settings"]
    target_resolution = resolution or getattr(settings, "render_resolution", "1920x1080")
    fps = getattr(settings, "render_fps", 30)
    crf = getattr(settings, "render_crf", 23)
    preset = getattr(settings, "render_preset", "medium")
    timeout = getattr(settings, "render_timeout_segment", 300)

    ffmpeg_ver = get_ffmpeg_version()
    click.echo(f"ffmpeg: {ffmpeg_ver}")
    click.echo(f"Target resolution: {target_resolution}, fps={fps}, crf={crf}")

    work_dir = tempfile.mkdtemp(prefix="btcedu_smoke_")
    click.echo(f"Working directory: {work_dir}")

    failed = False

    try:
        # Step 1: generate synthetic test video (testsrc2 → libx264/yuv420p)
        raw_video = str(Path(work_dir) / "raw_test.mp4")
        click.echo("\n[1/4] Generating synthetic test video via testsrc2...")
        try:
            generate_test_video(raw_video, duration=2.0, resolution=target_resolution, fps=fps)
            size = Path(raw_video).stat().st_size
            click.echo(f"  PASS  raw_test.mp4  ({size} bytes)")
        except Exception as e:
            click.echo(f"  FAIL  generate_test_video: {e}", err=True)
            failed = True

        if not failed:
            # Step 2: normalize_video_clip (scale/pad/yuv420p/fps normalization)
            norm_video = str(Path(work_dir) / "normalized.mp4")
            click.echo("[2/4] Running normalize_video_clip()...")
            try:
                normalize_video_clip(
                    input_path=raw_video,
                    output_path=norm_video,
                    resolution=target_resolution,
                    fps=fps,
                    crf=crf,
                    preset=preset,
                    timeout_seconds=timeout,
                )
                size = Path(norm_video).stat().st_size
                click.echo(f"  PASS  normalized.mp4  ({size} bytes)")
            except Exception as e:
                click.echo(f"  FAIL  normalize_video_clip: {e}", err=True)
                failed = True

        # Step 3: generate silent audio (anullsrc → aac)
        silent_audio = str(Path(work_dir) / "silent.m4a")
        click.echo("[3/4] Generating silent audio via anullsrc...")
        try:
            generate_silent_audio(silent_audio, duration=2.0)
            size = Path(silent_audio).stat().st_size
            click.echo(f"  PASS  silent.m4a  ({size} bytes)")
        except Exception as e:
            click.echo(f"  FAIL  generate_silent_audio: {e}", err=True)
            failed = True

        if not failed:
            # Step 4: create_video_segment (stream_loop + TTS audio + overlay pipeline)
            segment_out = str(Path(work_dir) / "segment.mp4")
            click.echo("[4/4] Running create_video_segment() with stream_loop...")
            try:
                create_video_segment(
                    video_path=norm_video,
                    audio_path=silent_audio,
                    output_path=segment_out,
                    duration=2.0,
                    overlays=[],
                    resolution=target_resolution,
                    fps=fps,
                    crf=crf,
                    preset=preset,
                    timeout_seconds=timeout,
                )
                # Validate output with ffprobe
                info = probe_media(segment_out)
                size = Path(segment_out).stat().st_size
                duration_ok = abs(info.duration_seconds - 2.0) < 0.5
                codec_ok = info.codec_video == "h264"
                click.echo(
                    f"  {'PASS' if (duration_ok and codec_ok) else 'WARN'}  segment.mp4  "
                    f"({size} bytes, duration={info.duration_seconds:.2f}s, "
                    f"codec={info.codec_video}, audio={info.codec_audio})"
                )
                if not duration_ok:
                    click.echo("  WARN  Output duration outside ±0.5s of target 2.0s", err=True)
                if not codec_ok:
                    click.echo(f"  WARN  Expected h264, got {info.codec_video}", err=True)
            except Exception as e:
                click.echo(f"  FAIL  create_video_segment: {e}", err=True)
                failed = True

    finally:
        if keep:
            click.echo(f"\nFiles retained at: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    if failed:
        click.echo("\n[FAIL] Smoke test failed — see errors above.", err=True)
        sys.exit(1)
    else:
        click.echo("\n[PASS] All smoke-test steps passed.")


@cli.command(context_settings={"allow_interspersed_args": False, "ignore_unknown_options": True})
@click.option("--json-only", is_flag=True, help="Output only the JSON summary.")
@click.option("--output", "-o", type=click.Path(), help="Write output to file instead of stdout.")
@click.pass_context
def llm_report(ctx: click.Context, json_only: bool, output: str | None) -> None:
    """Generate LLM provider introspection report.

    This command transparently reports which models and providers are accessible
    or known to the AI running in this production pipeline.

    Note: This command does not require database access.
    """
    from btcedu.utils.llm_introspection import format_full_report, generate_json_summary

    if json_only:
        content = json.dumps(generate_json_summary(), indent=2, ensure_ascii=False)
    else:
        content = format_full_report()

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        click.echo(f"Report written to: {output_path}")
    else:
        click.echo(content)


@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to publish to YouTube (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-publish even if already published.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Simulate upload without calling YouTube API.",
)
@click.option(
    "--privacy",
    type=click.Choice(["unlisted", "private", "public"]),
    default=None,
    help="Override privacy setting (default: youtube_default_privacy from config).",
)
@click.pass_context
def publish(
    ctx: click.Context,
    episode_ids: tuple[str, ...],
    force: bool,
    dry_run: bool,
    privacy: str | None,
) -> None:
    """Publish approved episode video to YouTube (v2 pipeline, Sprint 11)."""
    from btcedu.core.publisher import publish_video

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = publish_video(session, eid, settings, force=force, privacy=privacy)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already published at {result.youtube_url}")
                elif result.dry_run:
                    click.echo(f"[DRY-RUN] {eid} -> would publish (video_id={result.youtube_video_id})")
                else:
                    click.echo(f"[OK] {eid} -> {result.youtube_url}")
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()


@cli.command(name="youtube-auth")
@click.pass_context
def youtube_auth(ctx: click.Context) -> None:
    """Run OAuth2 authentication flow for YouTube Data API.

    Opens a browser window to authorize the app. Saves credentials to
    the path specified by youtube_credentials_path in config.
    """
    from btcedu.services.youtube_service import authenticate

    settings = ctx.obj["settings"]
    client_secrets = getattr(settings, "youtube_client_secrets_path", "data/client_secret.json")
    credentials_out = getattr(settings, "youtube_credentials_path", "data/.youtube_credentials.json")

    click.echo(f"Starting OAuth2 flow using: {client_secrets}")
    click.echo("A browser window will open to authorize this app...")
    try:
        authenticate(client_secrets_path=client_secrets, credentials_path=credentials_out)
        click.echo(f"[OK] Credentials saved to: {credentials_out}")
    except Exception as e:
        click.echo(f"[FAIL] Authentication failed: {e}", err=True)


@cli.command(name="youtube-status")
@click.pass_context
def youtube_status(ctx: click.Context) -> None:
    """Check YouTube API credential status and quota."""
    from btcedu.services.youtube_service import check_token_status

    settings = ctx.obj["settings"]
    credentials_path = getattr(settings, "youtube_credentials_path", "data/.youtube_credentials.json")

    try:
        status = check_token_status(credentials_path=credentials_path)
        has_creds = "error" not in status or "No credentials" not in status.get("error", "")
        click.echo(f"Credentials file : {credentials_path}")
        click.echo(f"Credentials exist: {has_creds}")
        if has_creds:
            click.echo(f"Token valid      : {status.get('valid', False)}")
            click.echo(f"Token expired    : {status.get('expired', 'N/A')}")
            click.echo(f"Expiry           : {status.get('expiry', 'N/A')}")
            click.echo(f"Can refresh      : {status.get('can_refresh', False)}")
        if "error" in status:
            click.echo(f"Error            : {status['error']}")
    except Exception as e:
        click.echo(f"[FAIL] Could not check status: {e}", err=True)

