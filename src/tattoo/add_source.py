"""tiny cli to seed sources until the settings interface lands (M6).

usage:
    PYTHONPATH=src uv run python -m tattoo.add_source web https://example.com/feed.xml "Example"
    PYTHONPATH=src uv run python -m tattoo.add_source youtube "https://www.youtube.com/feeds/videos.xml?channel_id=UC..." "Channel" --cap 5
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from tattoo import database

# defaults by type (plan §4): a news feed can produce fifty items a day,
# a channel a handful
DEFAULT_CAPS = {"web": 10, "youtube": 5}


def main() -> None:
    parser = argparse.ArgumentParser(description="add a source row")
    parser.add_argument("type", choices=["web", "youtube"])
    parser.add_argument("feed_url")
    parser.add_argument("display_name")
    parser.add_argument(
        "--cap", type=int, default=None, help="daily item cap (default by type)"
    )
    parser.add_argument("--site", default=None, help="site url")
    args = parser.parse_args()

    database.init_db()
    conn = database.connect()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    cap = args.cap if args.cap is not None else DEFAULT_CAPS[args.type]
    try:
        conn.execute(
            "INSERT INTO sources (type, feed_url, display_name, site_url, daily_item_cap,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (args.type, args.feed_url, args.display_name, args.site, cap, now, now),
        )
        conn.commit()
        print(f"added {args.type} source '{args.display_name}' (cap {cap}/day)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
