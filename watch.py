#!/usr/bin/env python3
"""
Watch a site's sitemaps for URLs that appear without ever being linked in navigation.

Shopify (and most CMSes) publish every live page into sitemap.xml in real time,
whether or not the page is reachable from the menu. That makes the sitemap the
highest-signal place to catch an unannounced page the moment it goes live.

Design notes worth keeping in mind before editing:

  * The origin sits behind Cloudflare and 403s datacenter IPs. Every fetch
    therefore falls through a chain of routes (direct -> r.jina.ai -> allorigins)
    and validates the body before trusting it.
  * A blocked fetch must NEVER be read as "the pages were deleted". Any section
    we could not fetch is carried forward from the previous state and reported
    as inconclusive, so a Cloudflare challenge can't manufacture a false alert.
  * Sub-sitemap URLs carry rotating ?from=&to= ids, so sections are keyed on the
    path only.

Stdlib only, no dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Substrings that mean we got an interstitial instead of the document.
CHALLENGE_MARKERS = (
    "challenge-platform",
    "_cf_chl_opt",
    "Verifying your connection",
    "Just a moment",
    "cf-browser-verification",
    "Enable JavaScript and cookies to continue",
)

# Consecutive fully-blocked runs before we raise a "the watcher itself is broken" alarm.
BLOCKED_STREAK_ALARM = 8


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """Date-only stamp. Keeping the committed state coarse means one commit a
    day instead of one every run -- which also keeps the repo active enough
    that GitHub never disables the cron for 60-day inactivity."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _http(url: str, extra_headers: dict | None = None, timeout: int = 60) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _route_direct(url: str) -> str:
    return _http(url)


def _route_jina(url: str) -> str:
    # Free text-extraction proxy. Egresses from its own IPs, which Cloudflare
    # lets through, and `x-respond-with: text` strips markup to bare text.
    return _http("https://r.jina.ai/" + url, extra_headers={"x-respond-with": "text"}, timeout=90)


def _route_allorigins(url: str) -> str:
    return _http("https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe=""))


ROUTES = (
    ("direct", _route_direct),
    ("r.jina.ai", _route_jina),
    ("allorigins", _route_allorigins),
)


def looks_blocked(body: str) -> bool:
    return any(m in body for m in CHALLENGE_MARKERS)


def fetch(url: str, log: list[str]) -> tuple[str | None, str | None]:
    """Return (body, route_name), or (None, None) if every route failed."""
    for name, fn in ROUTES:
        try:
            body = fn(url)
        except urllib.error.HTTPError as e:
            log.append(f"  {name}: HTTP {e.code}")
            continue
        except Exception as e:  # timeouts, DNS, TLS, proxy hiccups
            log.append(f"  {name}: {type(e).__name__}: {e}")
            continue
        if not body.strip():
            log.append(f"  {name}: empty body")
            continue
        if looks_blocked(body):
            log.append(f"  {name}: bot challenge")
            continue
        log.append(f"  {name}: ok ({len(body)} bytes)")
        return body, name
    return None, None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _child_text(node, tag: str) -> str | None:
    el = node.find(SM_NS + tag)
    if el is None:
        el = node.find(tag)
    if el is None or not el.text:
        return None
    return el.text.strip()


def _is_sitemap_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return "sitemap" in path and path.endswith(".xml")


def parse_sitemap(body: str, host: str) -> tuple[str, dict[str, str | None]]:
    """
    Return (kind, {url: lastmod_or_None}) where kind is "index" or "urlset".

    Raw XML is parsed properly. Proxy routes hand back tag-stripped text, so we
    fall back to pulling same-host URLs out with a regex; lastmod is simply
    unavailable on that path, which the diff treats as "unknown", not "changed".
    """
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        root = None

    if root is not None:
        kind = "index" if root.tag.split("}")[-1] == "sitemapindex" else "urlset"
        entries: dict[str, str | None] = {}
        for node in list(root):
            if node.tag.split("}")[-1] not in ("url", "sitemap"):
                continue
            loc = _child_text(node, "loc")
            if loc:
                entries[loc] = _child_text(node, "lastmod")
        return kind, entries

    entries = {}
    for m in re.finditer(r"https?://[^\s<>\"'()\[\]]+", body):
        url = m.group(0).rstrip(".,;")
        if urllib.parse.urlparse(url).netloc.endswith(host):
            entries.setdefault(url, None)
    if entries and all(_is_sitemap_url(u) for u in entries):
        return "index", entries
    # Strip the proxy's own echo of the document URL.
    return "urlset", {u: v for u, v in entries.items() if not _is_sitemap_url(u)}


def section_key(url: str) -> str:
    """Stable key ignoring the rotating ?from=&to= ids."""
    return urllib.parse.urlparse(url).path.strip("/") or "root"


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(name: str) -> dict:
    path = os.path.join(STATE_DIR, f"{name}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_state(name: str, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{name}.json"), "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def check_site(site: dict) -> dict:
    name = site["name"]
    index_url = site["sitemap"]
    host = urllib.parse.urlparse(index_url).netloc
    skip = [s.lower() for s in site.get("skip_sitemaps", [])]
    # Product/collection lastmod churns on every inventory tick, which would
    # rewrite state (and thus commit) every run. Only keep it where an edit
    # actually means something.
    lastmod_watch = [s.lower() for s in site.get("lastmod_watch", ["pages"])]

    state = load_state(name)
    prev_sections = state.get("sections", {})

    log: list[str] = [f"fetch {index_url}"]
    body, route = fetch(index_url, log)

    result = {
        "name": name,
        "host": host,
        "new": [],
        "removed": [],
        "changed_lastmod": [],
        "inconclusive": [],
        "log": log,
        "blocked": False,
        "probe_hits": [],
    }

    if body is None:
        # Total blackout. Do not touch the URL inventory.
        streak = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = streak
        state["last_failure_date"] = today()
        state.setdefault("sections", prev_sections)
        save_state(name, state)
        result["blocked"] = True
        result["blocked_streak"] = streak
        result["inconclusive"].append("sitemap index (every route failed)")
        return result

    kind, entries = parse_sitemap(body, host)
    if kind == "index":
        subs = [u for u in entries if not any(s in u.lower() for s in skip)]
    else:
        subs = [index_url]  # flat sitemap, no child documents

    fresh: dict[str, dict[str, str | None]] = {}
    for i, sub in enumerate(subs):
        if i:
            time.sleep(2)  # stay gentle; bursts are what tripped the WAF
        log.append(f"fetch {sub}")
        sub_body, _ = fetch(sub, log)
        key = section_key(sub)
        if sub_body is None:
            result["inconclusive"].append(key)
            if key in prev_sections:
                fresh[key] = prev_sections[key]  # carry forward, never diff a failure
            continue
        _, sub_entries = parse_sitemap(sub_body, host)
        if not sub_entries:
            result["inconclusive"].append(f"{key} (parsed empty)")
            if key in prev_sections:
                fresh[key] = prev_sections[key]
            continue
        track_lm = any(s in key.lower() for s in lastmod_watch)
        fresh[key] = {u: ({"lastmod": lm} if track_lm else {}) for u, lm in sub_entries.items()}

    # Optional slug probing, for pages deliberately excluded from the sitemap.
    for path in site.get("probe_paths", []):
        url = urllib.parse.urljoin(f"https://{host}", path)
        time.sleep(2)
        probe_body, _ = fetch(url, log)
        if probe_body is not None:
            result["probe_hits"].append(url)

    first_run = not prev_sections
    for key, urls in fresh.items():
        was_inconclusive = any(key == i.split(" ")[0] for i in result["inconclusive"])
        old = prev_sections.get(key, {})
        for url, meta in urls.items():
            if url not in old:
                if not first_run:
                    result["new"].append(url)
                meta["first_seen"] = today()
            else:
                meta["first_seen"] = old[url].get("first_seen", today())
                old_lm, new_lm = old[url].get("lastmod"), meta.get("lastmod")
                if old_lm and new_lm and old_lm != new_lm:
                    result["changed_lastmod"].append((url, old_lm, new_lm))
        if not was_inconclusive and not first_run:
            for url in old:
                if url not in urls:
                    result["removed"].append(url)

    state.update(
        {
            "site": name,
            "sitemap": index_url,
            "sections": fresh,
            "last_success_date": today(),
            "consecutive_failures": 0,
        }
    )
    state.setdefault("watching_since", today())
    save_state(name, state)
    result["first_run"] = first_run
    result["total_urls"] = sum(len(v) for v in fresh.values())
    result["route"] = route
    return result


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def render(results: list[dict]) -> tuple[str, bool, bool, str]:
    alert = any(r["new"] or r["removed"] or r["probe_hits"] for r in results)
    blocked_alarm = any(
        r["blocked"] and r.get("blocked_streak", 0) >= BLOCKED_STREAK_ALARM for r in results
    )

    all_new = [u for r in results for u in r["new"]] + [u for r in results for u in r["probe_hits"]]
    if all_new:
        head = os.path.basename(urllib.parse.urlparse(all_new[0]).path) or all_new[0]
        title = f"New page: /{head}" + (f" (+{len(all_new)-1} more)" if len(all_new) > 1 else "")
    elif blocked_alarm:
        title = "Watcher blocked — sitemap unreachable"
    else:
        title = "Site changed"

    # The title is built from a remote page slug, so it is untrusted: keep it on
    # one line and bounded before it can reach GITHUB_OUTPUT or a shell env var.
    title = re.sub(r"[\r\n]+", " ", title)[:120].strip()

    out = []
    for r in results:
        out.append(f"## {r['name']} — `{r['host']}`\n")
        if r["new"] or r["probe_hits"]:
            out.append("### 🚨 New URLs\n")
            for u in r["new"] + r["probe_hits"]:
                out.append(f"- **{u}**")
            out.append("")
        if r["removed"]:
            out.append("### Removed\n")
            out += [f"- {u}" for u in r["removed"]] + [""]
        if r["changed_lastmod"]:
            out.append("### Edited (lastmod moved)\n")
            out += [f"- {u}\n  - `{a}` → `{b}`" for u, a, b in r["changed_lastmod"]] + [""]
        if r["blocked"]:
            out.append(
                f"### ⚠️ Blocked\nEvery fetch route failed "
                f"({r.get('blocked_streak', 1)} run(s) in a row). "
                f"URL inventory left untouched.\n"
            )
        if r["inconclusive"]:
            out.append("### Inconclusive sections\n")
            out += [f"- {s} (carried forward, not diffed)" for s in r["inconclusive"]] + [""]
        if r.get("first_run"):
            out.append(f"_Baseline established: {r.get('total_urls', 0)} URLs recorded._\n")
        elif not (r["new"] or r["removed"] or r["changed_lastmod"] or r["blocked"]):
            out.append(f"No change. {r.get('total_urls', 0)} URLs tracked.\n")
        if r.get("route"):
            out.append(f"_Checked {now()} via `{r['route']}` route._\n")
        out.append("<details><summary>Fetch log</summary>\n\n```")
        out += r["log"]
        out.append("```\n</details>\n")

    return "\n".join(out), alert, blocked_alarm, title


def main() -> int:
    with open(os.path.join(ROOT, "config.json")) as f:
        config = json.load(f)

    only = sys.argv[1] if len(sys.argv) > 1 else None
    sites = [s for s in config["sites"] if not only or s["name"] == only]
    if not sites:
        print(f"no site named {only!r} in config.json", file=sys.stderr)
        return 2

    results = [check_site(s) for s in sites]
    report, alert, blocked_alarm, title = render(results)

    with open(os.path.join(ROOT, "report.md"), "w") as f:
        f.write(report)
    print(report)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"alert={'true' if alert else 'false'}\n")
            f.write(f"blocked={'true' if blocked_alarm else 'false'}\n")
            f.write(f"title={title}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
