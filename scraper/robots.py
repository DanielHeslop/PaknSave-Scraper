"""robots.txt guard shared by every scraper.

Each scraper fetches a site's robots.txt once per run (fetch_rules) and asks
RobotsRules.is_allowed(url) before every request. A disallowed URL is skipped
and logged; the run carries on. If robots.txt itself can't be fetched or read,
the guard fails open (allows everything) and logs that once - the guard must
never be the reason a run fails.

Matching follows the usual crawler convention (Google's, which urllib's
robotparser lacks): '*' and a trailing '$' work in patterns, the longest
matching rule wins, and Allow beats Disallow on a tie. Only the '*' user-agent
group is used. Paths are matched with their query string, because rules such
as /*?store= are about query strings.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15


class RobotsDisallowed(Exception):
    """A request was skipped because robots.txt disallows its URL."""


def _log(message: str) -> None:
    print(message, flush=True)


def _pattern_to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile("^" + body + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    """Parsed Allow/Disallow rules for one site. Empty rules allow everything."""

    rules: list[tuple[bool, str, re.Pattern]] = field(default_factory=list)  # (allow, pattern, regex)

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        rules: list[tuple[bool, str, re.Pattern]] = []
        group_agents: list[str] = []
        group_open = False  # still reading the User-agent lines of a group
        applies = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not group_open:
                    group_agents = []
                    group_open = True
                group_agents.append(value.lower())
                applies = "*" in group_agents
            elif key in ("allow", "disallow"):
                group_open = False
                if applies and value:
                    rules.append((key == "allow", value, _pattern_to_regex(value)))
            else:
                group_open = False
        return cls(rules)

    def is_allowed(self, url_or_path: str) -> bool:
        parts = urllib.parse.urlsplit(url_or_path)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        best_len = -1
        allowed = True
        for allow, pattern, regex in self.rules:
            if regex.match(path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len = length
                    allowed = allow
        return allowed


def fetch_rules(site_root: str, label: str, session: requests.Session | None = None) -> RobotsRules:
    """Fetch and parse <site_root>/robots.txt. Fails open: any problem logs one
    line and returns empty (allow-all) rules. Never raises."""
    url = site_root.rstrip("/") + "/robots.txt"
    try:
        getter = session.get if session is not None else requests.get
        response = getter(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        if response.status_code == 404:
            _log(f"robots.txt: {label} has none (404) - nothing is disallowed.")
            return RobotsRules()
        if response.status_code != 200:
            raise ValueError(f"status {response.status_code}")
        rules = RobotsRules.parse(response.text)
        _log(f"robots.txt: {label} loaded ({len(rules.rules)} rule(s) for all crawlers).")
        return rules
    except Exception as e:
        _log(f"robots.txt: could not read {label}'s ({type(e).__name__}: {e}) - continuing without the check.")
        return RobotsRules()


class RobotsGuard:
    """Per-run helper: caches one RobotsRules per site root and counts skips."""

    def __init__(self, fetcher=None):
        self._fetcher = fetcher or (lambda root, label: fetch_rules(root, label))
        self._rules: dict[str, RobotsRules] = {}
        self.skipped: list[str] = []

    def rules_for(self, site_root: str, label: str | None = None) -> RobotsRules:
        if site_root not in self._rules:
            self._rules[site_root] = self._fetcher(site_root, label or site_root)
        return self._rules[site_root]

    def allows(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if self.rules_for(root).is_allowed(url):
            return True
        _log(f"  SKIPPED (robots.txt disallows it): {urllib.parse.urlsplit(url).path}")
        self.skipped.append(url)
        return False
