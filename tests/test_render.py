"""page-set contract (plan §6): dated page, byte-identical dashboard copy,
archive index newest-first."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tattoo import config, render

TZ = ZoneInfo("America/Chicago")


def test_write_pages_produces_page_set(isolated_env):
    now = datetime(2026, 8, 6, 21, 0, tzinfo=TZ)
    dated = render.write_pages(now)

    dist = config.dist_path()
    assert dated == dist / "archive" / "2026-08-06" / "index.html"
    assert dated.exists()

    # dashboard is a byte-identical plain copy, not a symlink
    dashboard = dist / "dashboard" / "index.html"
    assert dashboard.read_bytes() == dated.read_bytes()
    assert not dashboard.is_symlink()

    # render date is prominent (stable url, changing content -- plan §6)
    html = dated.read_text()
    assert "2026-08-06" in html


def test_archive_index_lists_dates_newest_first(isolated_env):
    render.write_pages(datetime(2026, 8, 5, 21, 0, tzinfo=TZ))
    render.write_pages(datetime(2026, 8, 6, 21, 0, tzinfo=TZ))

    index = (config.dist_path() / "archive" / "index.html").read_text()
    assert index.index("2026-08-06") < index.index("2026-08-05")
