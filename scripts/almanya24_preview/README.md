# ALMANYA24 development acceptance preview

Source for the access-protected development preview. **Programs live here;
everything they produce stays under `data/almanya24-preview/` and is
git-ignored.** The two were in the same directory once, which made
`deploy/almanya24-dev-preview.service` unstartable from a clean checkout —
`tests/test_deploy_programs_are_committed.py` now prevents that from recurring.

Redirect the data root with `ALMANYA24_PREVIEW_DATA_DIR` (an absolute path);
the verification runs use it so they never touch the real acceptance data.

| Program | Purpose |
| --- | --- |
| `serve_preview.py` | Serves `<data>/site/current` on `127.0.0.1:8765`. The `ExecStart` target of the systemd unit. Directory listings are refused and only four named screenshots are reachable outside the site root. |
| `build_current_preview.py` | The bounded live harness: research, claim checking, Turkish drafting and media selection for the planned stories, under a hard call and budget guard. |
| `build_review_page.py` | Renders `<data>/site/current/_inceleme/index.html`: the stored, unapproved draft with its picture credit, claims, verdicts and source passages. |
| `build_preview.py` | The older fixture-only preview. No provider call, no network. |

## Runtime data (never committed)

`current-news.sqlite` and its `.bak` copies, `preview.sqlite`, `site/`,
`private/`, `current-private/`, `current-review/`, `current-source-cache/`,
`screenshots/`, `screenshot-pages/`, every `*-manifest.json`, `build-result.json`
and `current-news-plan.json`.

The plan is data, not source: it names the stories of one particular day with
their source URLs, publishers, publication and event dates and media queries.
Committing it would freeze one evening's editorial research into the repository
and date it immediately. `build_current_preview.py` reads it from
`<data>/current-news-plan.json`; its shape is

```json
{
  "prepared_at": "...", "timezone": "Europe/Berlin",
  "local_tagesschau_input": "...", "discovery": {...},
  "stories": [
    {
      "story_id": "...", "order": 1, "section": "...",
      "headline_de": "...", "source_url": "https://...",
      "publisher": "...", "published_at": "...", "event_date": "...",
      "media_query": "...", "media_subject": "...", "media_role": "symbol"
    }
  ]
}
```

## Cost

`build_current_preview.py` is the only one of the four that reaches a paid
provider; the other three make no paid call.

Its guard checks both a call cap and a cost ceiling **before every call**,
against the ledger, so the limits hold across resumed runs — but only
`--budget-usd` has a default (`2.0` USD). `--max-calls` is unlimited unless
given. Pass both explicitly for any real run.
