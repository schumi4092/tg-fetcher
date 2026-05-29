import sys

import pytest


@pytest.fixture
def client(fresh_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "AUTO_FETCH_INTERVAL_HOURS", 0)
    monkeypatch.setattr(config, "AUTO_SUMMARIZE_INTERVAL_HOURS", 0)
    monkeypatch.setattr(config, "API_ID", "")
    monkeypatch.setattr(config, "API_HASH", "")

    import db
    db.init_db()

    for mod in list(sys.modules):
        if mod in {"server", "routes"} or mod.startswith("routes."):
            sys.modules.pop(mod, None)
    import server
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_parse_trending_html_extracts_repo_fields():
    from routes.github_trending import parse_trending_html

    html = """
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/owner/repo"><svg></svg><span class="text-normal">owner /</span> repo</a>
      </h2>
      <p class="col-9 color-fg-muted my-1">Useful agent toolkit</p>
      <div class="f6 color-fg-muted mt-2">
        <span itemprop="programmingLanguage">Python</span>
        <a href="/owner/repo/stargazers">1,234</a>
        <a href="/owner/repo/forks">56</a>
        <span>789 stars today</span>
      </div>
    </article>
    """

    repos = parse_trending_html(html)

    assert repos == [{
        "rank": 1,
        "owner": "owner",
        "name": "repo",
        "full_name": "owner/repo",
        "url": "https://github.com/owner/repo",
        "description": "Useful agent toolkit",
        "language": "Python",
        "stars": 1234,
        "forks": 56,
        "period_stars": 789,
        "period_label": "today",
    }]


def test_github_trending_endpoint_uses_fetcher(client, monkeypatch):
    import routes.github_trending as gh

    monkeypatch.setattr(gh, "_cache", {})
    monkeypatch.setattr(gh, "_fetch_trending", lambda since, language, limit: [{
        "rank": 1,
        "owner": "owner",
        "name": "repo",
        "full_name": "owner/repo",
        "url": "https://github.com/owner/repo",
        "description": "Useful agent toolkit",
        "language": "Python",
        "stars": 1234,
        "forks": 56,
        "period_stars": 789,
        "period_label": "today",
    }])

    r = client.get("/api/github/trending?since=daily&language=python&limit=1")

    assert r.status_code == 200
    data = r.get_json()
    assert data["since"] == "daily"
    assert data["language"] == "python"
    assert data["repos"][0]["full_name"] == "owner/repo"
