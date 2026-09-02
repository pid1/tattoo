"""render the briefing page set (plan §6).

writes the dated archive page, copies identical bytes to the dashboard
(plain copy, not a symlink or meta-refresh -- no failure modes, saves
offline correctly), and rebuilds the archive index. jinja2, server-side
only; the output is static self-contained html with no javascript.

M0 renders a placeholder body; real briefing content arrives with M1+.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tattoo import config

# nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
# autoescape is on via select_autoescape below; the rule fires on any
# Environment() regardless of how it is configured.
_env = Environment(
    loader=FileSystemLoader(config.REPO_ROOT / "templates"),
    autoescape=select_autoescape(["html"]),
)


def write_pages(now: datetime, context: dict | None = None) -> Path:
    """render the dated page, mirror it to the dashboard, refresh the
    archive index. returns the dated page path."""
    dist = config.dist_path()
    date_str = now.strftime("%Y-%m-%d")

    ctx = {
        "render_date": date_str,
        "rendered_at": now.strftime("%Y-%m-%d %H:%M %Z"),
        "sections": [],
    }
    ctx.update(context or {})

    dated_dir = dist / "archive" / date_str
    dated_dir.mkdir(parents=True, exist_ok=True)
    dated_page = dated_dir / "index.html"
    dated_page.write_text(_env.get_template("briefing.html").render(**ctx), encoding="utf-8")

    dashboard_dir = dist / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dated_page, dashboard_dir / "index.html")

    _write_archive_index(dist)
    return dated_page


def _write_archive_index(dist: Path) -> None:
    archive = dist / "archive"
    dates = sorted((p.name for p in archive.iterdir() if p.is_dir()), reverse=True)
    html = _env.get_template("archive_index.html").render(dates=dates)
    (archive / "index.html").write_text(html, encoding="utf-8")
