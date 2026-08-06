"""http surface smoke tests (rally's test_pages.py pattern)."""

from tattoo import pipeline


def test_root_redirects_to_dashboard(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/dashboard/"


def test_dashboard_serves_rendered_page(client):
    pipeline.run("manual")
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "tattoo" in resp.text


def test_static_responses_force_revalidation(client):
    # stable url, daily-changing bytes: no-cache is load-bearing (plan §6)
    pipeline.run("manual")
    resp = client.get("/dashboard/")
    assert resp.headers["cache-control"] == "no-cache"


def test_archive_index_served(client):
    pipeline.run("manual")
    resp = client.get("/archive/")
    assert resp.status_code == 200
    assert "archive" in resp.text
