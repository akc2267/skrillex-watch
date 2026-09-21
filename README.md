# sitemap-watch

Alerts you when a page appears on a website that isn't linked from anywhere in its navigation.

Built for the case that motivated it: Skrillex announced a Chicago popup whose only
signup lived at `skrillex.com/pages/chicago26`, reachable by direct link only.

## Why this works

`skrillex.com` runs on Shopify, and Shopify writes **every published page** into
`sitemap.xml` in real time — whether or not the page is in a menu. Verified:

```
$ curl -s 'https://skrillex.com/sitemap_pages_1.xml?from=...' | grep chicago
    <loc>https://skrillex.com/pages/chicago26</loc>   lastmod 2026-09-08
```

The page nobody could navigate to was sitting in the sitemap the whole time. So we
snapshot the sitemap every 15 minutes and shout when a URL shows up that wasn't
there before. The same holds for WooCommerce, Squarespace, Webflow, Wix and most
of Shopify's competitors — point `config.json` at any of them.

## The part that took the actual work

The origin is behind Cloudflare, which 403s datacenter IPs — and GitHub Actions
runners are datacenter IPs. Two consequences the code handles:

1. **Fallback routes.** Each fetch tries `direct` → `r.jina.ai` → `allorigins`,
   validating the body each time. `r.jina.ai` is confirmed to get through and is
   free with no key.
2. **A block is never read as a deletion.** This is the important one. A naive
   differ sees a Cloudflare challenge, parses zero URLs, and reports that the
   entire site was deleted. Here, any section we couldn't fetch is carried
   forward from the last good snapshot and reported as *inconclusive* — never
   diffed. Eight consecutive total failures raise a separate "watcher is broken"
   issue, so silence is never mistaken for "nothing happened".

Requests are also spaced 2s apart, which was enough to stay under the WAF
threshold that blocked a burst of 8 during development.

## Setup

```bash
gh repo create sitemap-watch --public --source=. --push
```

Make it **public** — public repos get unlimited free Actions minutes. (A private
repo burns the 2,000 free minutes/month at this cadence.)

Then: **Settings → Actions → General → Workflow permissions → Read and write**,
and confirm the schedule is live under the Actions tab.

That's it. Alerts arrive as **GitHub issues**, which email you automatically as
long as you're watching the repo.

### Optional phone push

| Secret | How |
| --- | --- |
| `NTFY_TOPIC` | Install [ntfy](https://ntfy.sh), subscribe to a hard-to-guess topic, set the secret to that topic name. Free, no account. |
| `DISCORD_WEBHOOK` | Channel → Edit → Integrations → New Webhook → copy URL. |

Both steps are skipped when the secret is absent.

## Adding sites

```jsonc
{
  "sites": [
    {
      "name": "skrillex",
      "sitemap": "https://skrillex.com/sitemap.xml",
      "skip_sitemaps": ["agentic_discovery"],  // permanently Cloudflare-walled
      "lastmod_watch": ["pages"],              // report edits only for these sections
      "probe_paths": []                        // see below
    }
  ]
}
```

`lastmod_watch` matters: product `lastmod` churns every few seconds from inventory
updates, so tracking it would rewrite state — and commit — on every single run.

### `probe_paths`, for pages hidden *from the sitemap*

A page can be excluded from the sitemap with a `noindex` metafield. `chicago26`
wasn't, but a future one might be. `probe_paths` just fetches guessed slugs and
alerts on anything that isn't a 404:

```json
"probe_paths": ["/pages/la26", "/pages/nyc26", "/pages/miami26"]
```

Keep the list short — each entry is another request against the WAF.

## Running it yourself

```bash
python3 watch.py            # check every site in config.json
python3 watch.py skrillex   # just one
python3 selftest.py         # offline tests, no network
```

`selftest.py` covers the failure modes that would otherwise produce false alerts:
total block, partial block, empty body, recovery, genuine removal, and the
tag-stripped output the proxy routes return.

## Known limits

- **Cron drift.** GitHub's scheduler is best-effort; a 15-minute cron can land
  20–40 minutes late under load. Fine for an event announced hours ahead, not a
  ticket-drop sniper.
- **Actions disables cron after 60 days of repo inactivity.** Handled: the state
  file stores a date rather than a timestamp, so it commits about once a day —
  frequent enough to keep the schedule alive, quiet enough not to spam.
- **Sitemap lag.** Shopify's is effectively instant, but a CMS that caches its
  sitemap will delay detection by however long it caches.
- If `r.jina.ai` ever starts requiring a key, add a route to `ROUTES` in
  `watch.py` — it's a list of `(name, fn)` pairs.
