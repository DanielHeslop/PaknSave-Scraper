from scraper.robots import RobotsGuard, RobotsRules, fetch_rules

FOODSTUFFS = """User-agent: *
Disallow: /*?utm
Disallow: /search
Disallow: /*?store=
Disallow: /shop/search
Disallow: /shop/sitecore/

Disallow: /*?ShowDesktopSite=
Sitemap: https://x/y.xml
"""


def test_foodstuffs_rules():
    r = RobotsRules.parse(FOODSTUFFS)
    assert r.is_allowed("https://www.paknsave.co.nz/shop/category/pantry/bulk-foods")
    assert r.is_allowed("https://www.paknsave.co.nz/shop/category/vegetables?pg=2")
    assert not r.is_allowed("https://www.paknsave.co.nz/shop/search?q=milk&sf=shopping")
    assert not r.is_allowed("/search")
    assert not r.is_allowed("https://www.paknsave.co.nz/shop/category/pantry?store=abc")
    assert not r.is_allowed("/shop/x?utm_campaign=1")


def test_allow_overrides_shorter_disallow_and_dollar_anchor():
    r = RobotsRules.parse("User-agent: *\nDisallow: /a\nAllow: /a/ok\nDisallow: /*.pdf$\n")
    assert not r.is_allowed("/a/blocked")
    assert r.is_allowed("/a/ok/page")
    assert not r.is_allowed("/files/x.pdf")
    assert r.is_allowed("/files/x.pdf?download=1")


def test_only_star_group_applies_and_groups_with_several_agents():
    text = "User-agent: Googlebot\nDisallow: /\n\nUser-agent: other\nUser-agent: *\nDisallow: /x\n"
    r = RobotsRules.parse(text)
    assert r.is_allowed("/y")
    assert not r.is_allowed("/x/1")


def test_empty_disallow_and_empty_file_allow_everything():
    assert RobotsRules.parse("User-agent: *\nDisallow:\n").is_allowed("/anything")
    assert RobotsRules.parse("").is_allowed("/anything")


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_fetch_rules_fails_open(monkeypatch, capsys):
    import scraper.robots as robots

    monkeypatch.setattr(robots.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    rules = fetch_rules("https://example.com", "Example")
    assert rules.is_allowed("/search")
    assert "continuing without the check" in capsys.readouterr().out

    monkeypatch.setattr(robots.requests, "get", lambda *a, **k: _Resp(500))
    assert fetch_rules("https://example.com", "Example").is_allowed("/search")
    monkeypatch.setattr(robots.requests, "get", lambda *a, **k: _Resp(404))
    assert fetch_rules("https://example.com", "Example").is_allowed("/search")
    monkeypatch.setattr(robots.requests, "get", lambda *a, **k: _Resp(200, FOODSTUFFS))
    assert not fetch_rules("https://example.com", "Example").is_allowed("/search")


def test_guard_fetches_once_per_site_and_records_skips(capsys):
    calls = []

    def fetcher(root, label):
        calls.append(root)
        return RobotsRules.parse(FOODSTUFFS)

    g = RobotsGuard(fetcher)
    assert g.allows("https://a.example/shop/category/x")
    assert g.allows("https://a.example/shop/category/y")
    assert not g.allows("https://a.example/shop/search?q=milk")
    assert g.allows("https://b.example/shop/search?q=milk") is False
    assert calls == ["https://a.example", "https://b.example"]
    assert len(g.skipped) == 2
    assert "robots.txt disallows" in capsys.readouterr().out
