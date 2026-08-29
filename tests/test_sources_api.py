"""source lifecycle api (plan §8): create/edit, disable as the reversible
remove, purge as the guarded cascade, stats on the list view."""

from datetime import UTC, datetime


def _create(client, **overrides):
    body = {
        "type": "web",
        "feed_url": "https://example.com/feed.xml",
        "display_name": "Example",
    }
    body.update(overrides)
    return client.post("/api/sources", json=body)


def test_create_applies_type_defaults(client):
    resp = _create(client)
    assert resp.status_code == 201
    src = resp.json()
    assert src["daily_item_cap"] == 10  # web default
    assert src["threshold"] == 5
    assert src["enabled"] is True

    yt = _create(
        client,
        type="youtube",
        feed_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCx",
    ).json()
    assert yt["daily_item_cap"] == 5  # youtube default


def test_duplicate_feed_url_conflicts(client):
    _create(client)
    assert _create(client).status_code == 409


def test_update_criteria_threshold_cap(client):
    src = _create(client).json()
    resp = client.put(
        f"/api/sources/{src['id']}",
        json={"criteria": "only rf content", "threshold": 7, "daily_item_cap": 3},
    )
    updated = resp.json()
    assert (updated["criteria"], updated["threshold"], updated["daily_item_cap"]) == (
        "only rf content",
        7,
        3,
    )


def test_disable_is_reversible_and_keeps_rows(client, db):
    src = _create(client).json()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
        " first_seen_at) VALUES (?, 'x', 'u', 't', 't', ?)",
        (src["id"], now),
    )
    db.commit()

    disabled = client.put(f"/api/sources/{src['id']}", json={"enabled": False}).json()
    assert disabled["enabled"] is False
    assert (
        db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1
    )  # history kept

    assert client.put(f"/api/sources/{src['id']}", json={"enabled": True}).json()[
        "enabled"
    ]


def test_purge_cascades(client, db):
    src = _create(client).json()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
        " first_seen_at) VALUES (?, 'x', 'u', 't', 't', ?)",
        (src["id"], now),
    )
    db.execute(
        "INSERT INTO content (item_id, text, method, fetched_at) VALUES (1, 't', 'feed_body', ?)",
        (now,),
    )
    db.commit()

    assert client.delete(f"/api/sources/{src['id']}").status_code == 204
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"] == 0


def test_non_editable_field_rejected(client):
    src = _create(client).json()
    resp = client.put(
        f"/api/sources/{src['id']}", json={"feed_url": "https://other/feed"}
    )
    assert resp.status_code == 400


def test_list_includes_stats(client):
    _create(client)
    listing = client.get("/api/sources").json()["sources"]
    assert listing[0]["stats"] == {
        "items_total": 0,
        "judged_30d": 0,
        "pass_rate_30d": None,
        "last_passing_at": None,
        "tokens_30d": 0,
    }


def test_missing_source_404s(client):
    assert client.get("/api/sources/99").status_code == 404
    assert client.put("/api/sources/99", json={"threshold": 5}).status_code == 404
    assert client.delete("/api/sources/99").status_code == 404
