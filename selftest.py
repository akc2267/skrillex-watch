#!/usr/bin/env python3
"""Offline tests for the failure modes that would otherwise produce false alerts.

No network. Routes are stubbed so we can force Cloudflare challenges on demand.
Run: python3 selftest.py
"""
import json
import os
import shutil
import tempfile

import watch

CHALLENGE = '<html><head><title>Just a moment...</title></head><body>' \
            '<script>window._cf_chl_opt={};</script>challenge-platform</body></html>'

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap_pages_1.xml?from=1&amp;to=2</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap_products_1.xml?from=3&amp;to=4</loc></sitemap>
</sitemapindex>"""

def urlset(paths):
    body = "".join(
        f"<url><loc>https://example.com{p}</loc><lastmod>2026-01-0{i+1}T00:00:00Z</lastmod></url>"
        for i, p in enumerate(paths))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>')

SITE = {"name": "t", "sitemap": "https://example.com/sitemap.xml", "lastmod_watch": ["pages"]}

corpus, blocked_paths = {}, set()

def fake_route(url):
    for frag in blocked_paths:
        if frag in url:
            return CHALLENGE
    for key, body in corpus.items():
        if key in url:
            return body
    raise RuntimeError("404")

def setup(pages, products=("/products/a",)):
    corpus.clear()
    corpus["sitemap.xml?"] = INDEX
    corpus["/sitemap.xml"] = INDEX
    corpus["sitemap_pages_1"] = urlset(pages)
    corpus["sitemap_products_1"] = urlset(list(products))

def urls_in_state():
    st = json.load(open(os.path.join(watch.STATE_DIR, "t.json")))
    return {u for sec in st["sections"].values() for u in sec}

FAILED = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(label)

def main():
    watch.ROUTES = (("stub", fake_route),)
    watch.time.sleep = lambda *_: None
    tmp = tempfile.mkdtemp()
    watch.STATE_DIR = tmp
    try:
        print("\n1. baseline run records URLs and stays silent")
        setup(["/pages/tour"])
        r = watch.check_site(SITE)
        check("no alert on first run", not r["new"] and not r["removed"])
        check("inventory recorded", urls_in_state() >= {"https://example.com/pages/tour"})

        print("\n2. a new unlinked page is detected")
        setup(["/pages/tour", "/pages/chicago26"])
        r = watch.check_site(SITE)
        check("new page reported", r["new"] == ["https://example.com/pages/chicago26"], r["new"])
        check("nothing falsely removed", not r["removed"], r["removed"])

        print("\n3. TOTAL Cloudflare block -> no removals, inventory preserved")
        before = urls_in_state()
        blocked_paths.add("example.com")
        r = watch.check_site(SITE)
        check("flagged as blocked", r["blocked"])
        check("NO removals invented", not r["removed"], r["removed"])
        check("NO new invented", not r["new"], r["new"])
        check("inventory intact", urls_in_state() == before)
        blocked_paths.clear()

        print("\n4. repeated blocks escalate to a watcher-broken alarm")
        blocked_paths.add("example.com")
        for _ in range(watch.BLOCKED_STREAK_ALARM - 1):
            r = watch.check_site(SITE)
        _, alert, blocked_alarm, title = watch.render([r])
        check(f"alarm after {watch.BLOCKED_STREAK_ALARM} failures", blocked_alarm, str(r.get("blocked_streak")))
        check("blocked alarm is not a page alert", not alert)
        blocked_paths.clear()

        print("\n5. recovery clears the failure streak")
        setup(["/pages/tour", "/pages/chicago26"])
        r = watch.check_site(SITE)
        st = json.load(open(os.path.join(tmp, "t.json")))
        check("streak reset to 0", st["consecutive_failures"] == 0)
        check("no spurious diff on recovery", not r["new"] and not r["removed"], f"{r['new']} {r['removed']}")

        print("\n6. PARTIAL block -> that section carried forward, others still diffed")
        setup(["/pages/tour", "/pages/chicago26"], products=["/products/a", "/products/NEW"])
        blocked_paths.add("sitemap_pages_1")
        r = watch.check_site(SITE)
        check("pages marked inconclusive", any("pages" in s for s in r["inconclusive"]), str(r["inconclusive"]))
        check("no removals from blocked section", not r["removed"], r["removed"])
        check("pages inventory carried forward",
              "https://example.com/pages/chicago26" in urls_in_state())
        check("healthy section still detected new product",
              r["new"] == ["https://example.com/products/NEW"], r["new"])
        blocked_paths.clear()

        print("\n7. a genuine removal (clean fetch) IS reported")
        setup(["/pages/tour"], products=["/products/a", "/products/NEW"])
        r = watch.check_site(SITE)
        check("removal reported", r["removed"] == ["https://example.com/pages/chicago26"], r["removed"])

        print("\n8. garbage/empty body is treated as inconclusive, not as deletion")
        setup(["/pages/tour"], products=["/products/a", "/products/NEW"])
        corpus["sitemap_pages_1"] = "   "
        r = watch.check_site(SITE)
        check("empty body -> inconclusive", any("pages" in s for s in r["inconclusive"]), str(r["inconclusive"]))
        check("no removals from empty body", not r["removed"], r["removed"])

        print("\n9. tag-stripped proxy output still yields URLs")
        kind, entries = watch.parse_sitemap(
            "\n  https://example.com/pages/tour\n  https://example.com/pages/secret\n", "example.com")
        check("text fallback parses", kind == "urlset" and len(entries) == 2, f"{kind} {entries}")
        kind2, _ = watch.parse_sitemap(
            "\n https://example.com/sitemap_pages_1.xml?from=1&to=2\n", "example.com")
        check("text fallback spots an index", kind2 == "index", kind2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("ALL TESTS PASSED" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    return 1 if FAILED else 0

if __name__ == "__main__":
    raise SystemExit(main())
