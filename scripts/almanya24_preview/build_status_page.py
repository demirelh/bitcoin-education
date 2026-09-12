"""The operations page: what the timer did, what it cost, what broke.

Written into the live site directory as ``/_durum/`` so it sits behind the
same basic auth as the rest of the development preview. It is deliberately a
separate page from the articles: an operational state like "budget exhausted"
is not news, and putting it on a news card was the exact mistake this preview
made before.
"""

from __future__ import annotations

import html
import json
import subprocess
from datetime import datetime
from pathlib import Path

from btcedu.core.editorial.daily import DailyPaths, RunOutcome, load_report

OUTCOME_LABELS = {
    RunOutcome.SUCCESS.value: ("Başarılı çalışma", "ok"),
    RunOutcome.NO_NEW_TRANSCRIPT.value: ("Yeni transkript yok", "idle"),
    RunOutcome.NO_SUITABLE_TOPICS.value: ("Uygun konu yok", "idle"),
    RunOutcome.BUDGET_NOT_APPROVED.value: ("Bütçe onaylanmadı", "blocked"),
    RunOutcome.BUDGET_EXHAUSTED.value: ("Bütçe tükendi", "blocked"),
    RunOutcome.ERROR.value: ("Teknik hata", "error"),
    RunOutcome.ALREADY_RUNNING.value: ("Zaten çalışıyor", "idle"),
    RunOutcome.NEEDS_EDITORIAL_DECISION.value: ("Editör kararı bekleniyor", "blocked"),
}

STORY_LABELS = {
    "drafted": "Taslak oluşturuldu",
    "failed": "Başarısız",
    "waiting_for_budget": "Bütçe bekliyor",
    "needs_decision": "Editör kararı bekliyor",
}

TIMER_UNIT = "almanya24-daily.timer"

STYLE = """
body{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f4f5f7;color:#15202b}
main{max-width:960px;margin:0 auto;padding:1.5rem 1.25rem 4rem}
h1{font-size:1.6rem;margin:0 0 .25rem}
.sub{color:#5a6672;margin:0 0 1.5rem}
.state{display:inline-block;padding:.3rem .7rem;border-radius:3px;font-weight:700;
 font-size:.85rem;text-transform:uppercase;letter-spacing:.03em}
.state.ok{background:#1e8449;color:#fff}
.state.idle{background:#5d6d7e;color:#fff}
.state.blocked{background:#b9770e;color:#fff}
.state.error{background:#c0392b;color:#fff}
section{background:#fff;border:1px solid #dfe3e8;border-radius:4px;padding:1rem 1.1rem;
 margin:0 0 1rem}
h2{font-size:1.05rem;margin:0 0 .6rem;text-transform:uppercase;letter-spacing:.04em;
 color:#5a6672}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:.4rem .5rem;border-bottom:1px solid #eceff1;
 vertical-align:top}
th{color:#5a6672;font-weight:600;white-space:nowrap}
.bar{background:#eceff1;border-radius:3px;height:10px;overflow:hidden;margin:.3rem 0}
.bar>span{display:block;height:100%;background:#1e8449}
.bar.warn>span{background:#b9770e}
.bar.full>span{background:#c0392b}
code{background:#eceff1;padding:.1rem .3rem;border-radius:2px;font-size:.86rem}
.err{color:#c0392b}
@media(max-width:600px){th,td{padding:.35rem .3rem;font-size:.86rem}}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _next_run() -> str:
    """Ask systemd rather than recompute the schedule from the unit file."""
    try:
        out = subprocess.run(
            ["systemctl", "list-timers", "--no-pager", "--no-legend", TIMER_UNIT],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    line = out.stdout.strip()
    return line.split("  ")[0].strip() if line else ""


def _bar(used: float, limit: float) -> str:
    if limit <= 0:
        return '<div class="bar full"><span style="width:100%"></span></div>'
    share = min(1.0, used / limit)
    css = "bar" + (" full" if share >= 1 else " warn" if share >= 0.8 else "")
    return f'<div class="{css}"><span style="width:{share * 100:.1f}%"></span></div>'


def render(report: dict | None, *, next_run: str = "") -> str:
    if report is None:
        return _page(
            "<section><h2>Durum</h2><p>Henüz bir otomatik çalışma kaydı yok.</p>"
            "</section>"
        )
    outcome = str(report.get("outcome", ""))
    label, css = OUTCOME_LABELS.get(outcome, (outcome or "bilinmiyor", "idle"))
    limits = report.get("limits", {})
    usage = report.get("usage", {})
    spent = float(usage.get("spent_usd", 0.0))
    budget = float(limits.get("budget_usd", 0.0))
    calls = int(usage.get("calls", 0))
    max_calls = int(limits.get("max_calls", 0))

    blocks = [
        f'<section><h2>Son çalışma</h2>'
        f'<p><span class="state {css}">{_e(label)}</span></p>'
        "<table>"
        f"<tr><th>Başlangıç</th><td>{_e(report.get('started_at', ''))}</td></tr>"
        f"<tr><th>Bitiş</th><td>{_e(report.get('finished_at', ''))}</td></tr>"
        f"<tr><th>Transkript</th><td>{_e(report.get('transcript') or '—')}</td></tr>"
        f"<tr><th>Yayın tarihi</th>"
        f"<td>{_e(report.get('broadcast_date') or '—')}</td></tr>"
        f"<tr><th>Kaynak</th><td>{_e(report.get('attribution') or '—')}</td></tr>"
        f"<tr><th>Sitede görünen</th><td>{_e(report.get('published', 0))}</td></tr>"
        f"<tr><th>Sonraki çalışma</th><td>{_e(next_run or '—')}</td></tr>"
        "</table></section>"
    ]

    blocks.append(
        "<section><h2>Günlük tüketim ve sınırlar</h2>"
        "<table>"
        f"<tr><th>Gün</th><td>{_e(usage.get('day', ''))}</td></tr>"
        f"<tr><th>Harcama</th><td>{spent:.4f} / {budget:.4f} USD"
        f"{_bar(spent, budget)}</td></tr>"
        f"<tr><th>Sağlayıcı çağrısı</th><td>{calls} / {max_calls}"
        f"{_bar(calls, max_calls)}</td></tr>"
        f"<tr><th>Azami haber</th><td>{_e(limits.get('max_stories', 0))}</td></tr>"
        "</table>"
        + (
            "<p><strong>Ücretli çağrılar kilitli.</strong> Günlük bütçe veya çağrı "
            "sınırı sıfır olduğu için model çağrısı yapılmaz.</p>"
            if budget <= 0 or max_calls <= 0
            else ""
        )
        + "</section>"
    )

    stories = report.get("stories", [])
    if stories:
        rows = "".join(
            "<tr>"
            f"<td>{_e(item.get('headline_de', ''))}</td>"
            f"<td>{_e(STORY_LABELS.get(item.get('status', ''), item.get('status', '')))}</td>"
            f"<td>{_e(item.get('title_tr') or '—')}</td>"
            f"<td class=\"err\">{_e(item.get('detail') or '')}</td>"
            "</tr>"
            for item in stories
        )
        blocks.append(
            "<section><h2>Bu çalışmadaki haberler</h2><table>"
            "<tr><th>Yayındaki başlık (DE)</th><th>Durum</th>"
            "<th>Türkçe başlık</th><th>Ayrıntı</th></tr>"
            f"{rows}</table></section>"
        )

    errors = report.get("errors", [])
    if errors:
        blocks.append(
            "<section><h2>Hatalar</h2><ul>"
            + "".join(f'<li class="err"><code>{_e(line)}</code></li>' for line in errors)
            + "</ul></section>"
        )

    totals = usage.get("processed_total", {})
    if totals:
        blocks.append(
            "<section><h2>Toplam işlenen haberler</h2><table>"
            + "".join(
                f"<tr><th>{_e(STORY_LABELS.get(key, key))}</th><td>{_e(value)}</td></tr>"
                for key, value in sorted(totals.items())
            )
            + "</table></section>"
        )

    return _page("\n".join(blocks))


def _page(body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"tr\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>ALMANYA24 · İşletim durumu</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n<main>\n"
        "<h1>ALMANYA24 · İşletim durumu</h1>\n"
        '<p class="sub">Korumalı geliştirme önizlemesi · '
        f"oluşturma: {_e(datetime.now().isoformat(timespec='seconds'))}</p>\n"
        f"{body}\n</main>\n</body>\n</html>\n"
    )


def build(paths: DailyPaths) -> Path:
    report = load_report(paths.report)
    out = paths.site / "current" / "_durum" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report, next_run=_next_run()), encoding="utf-8")
    return out


def main() -> None:
    if __package__:
        from .paths import data_root
    else:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from paths import data_root

    path = build(DailyPaths(data_dir=data_root()))
    print(json.dumps({"status_page": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
