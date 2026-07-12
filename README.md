# bruh, just check the market

A personal, economy-wide pain-discovery instrument. Not a product.
It mines Reddit + Hacker News for one specific shape of opportunity:

> A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is the shape of an agentic product hiding in the open. Claude reads each
candidate through one opinionated founder filter; every result carries its
**industry** and **where those people gather**, so the output doubles as an
outreach target list. It runs itself every 2 days and publishes a filterable
dashboard to GitHub Pages - no live server, no running cost.

## What v2 adds

- **Whole economy, not just tech.** [`industries.py`](./industries.py) is a taxonomy of
  **36 GDP sectors / 166 operator subreddits** - agriculture, trucking, dental, legal,
  oil & gas, funeral homes, customs compliance, and on. Every finding is tagged with an `industry`.
- **Memory.** [`data/findings.json`](./data) accumulates across runs (dedup by URL) and tracks
  `times_seen` - a pain that keeps resurfacing is a stronger signal. The dashboard grows into a
  standing board instead of a throwaway snapshot.
- **Hosted dashboard.** A GitHub Action runs the scan every 2 days and deploys a redesigned,
  filterable dashboard (summary cards, live filters by verdict / industry / wtp-tier / source,
  full-text search) to GitHub Pages.

## The pipeline

```
load memory -> fetch (Reddit + HN) -> prefilter -> skip already-seen
  -> Claude judgment (new items only) -> competition check (web search, high-fit only)
  -> merge into memory -> render dashboard -> open
```

Two files do the work: [`ask_my_market.py`](./ask_my_market.py) (logic) and
[`industries.py`](./industries.py) (the sector/subreddit/query taxonomy - edit freely).

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python ask_my_market.py                 # incremental run -> writes + opens report.html
python ask_my_market.py --dry-run       # fetch + prefilter only, zero API cost
python ask_my_market.py --sample --limit 20   # fast smoke test (tiny source subset)
```

Useful flags: `--limit N` (max NEW items classified), `--data PATH` (memory file),
`--out PATH` (dashboard path), `--no-open`.

## Host it on GitHub Pages (zero running cost)

One-time setup:

1. **Add your API key as a repo secret** (uploads your key to *your* repo's secrets):
   ```bash
   gh secret set ANTHROPIC_API_KEY --repo DavidKorochik/bruh-just-check-the-market
   ```
2. **Turn on Pages -> GitHub Actions** (Settings -> Pages -> Source: GitHub Actions), or:
   ```bash
   gh api -X POST repos/DavidKorochik/bruh-just-check-the-market/pages -f build_type=workflow
   ```
3. **Kick off the first run** (populates the memory + dashboard; ~166 RSS fetches + classification,
   a few minutes):
   ```bash
   gh workflow run scan.yml
   ```

After that, [`.github/workflows/scan.yml`](./.github/workflows/scan.yml) runs every 2 days:
scans (only new items are classified), commits the updated memory, and deploys the dashboard.
Your link: `https://davidkorochik.github.io/bruh-just-check-the-market/`.

> The Pages site is **public** (public repo). It shows public Reddit/HN data plus your scoring.

## How scoring works

Claude returns the *judgment* fields (`wtp_tier`, `judgment_on_stream`, `industry`, ...).
`fit_score` and `verdict` are computed **in Python** from the `WEIGHTS` dict in
`ask_my_market.py`, so the math is deterministic and you tune scoring in one place.
`paying_a_human` is weighted hardest - money already moving to a human is the strongest
signal an agent can take the job.

| verdict        | fit_score |
|----------------|-----------|
| `worth_a_call` | >= 65     |
| `watch`        | 40 - 64   |
| `skip`         | < 40      |

## Competition check (is the market already crowded?)

A real pain that strong incumbents already serve is a trap, not an opportunity. So for every
**high-fit** idea (`worth_a_call` / `watch` only - `skip`s are ignored to bound cost), the tool asks
Claude to **live web-search** for existing products, then **sanity-check the search against its own
prior knowledge** (and lower its confidence if they disagree). Each idea gets:

- `competition_level`: `open_field` → `some_players` → `crowded` → `saturated`
- the named `competitors` it found, a one-line rationale, and a confidence

This is **flag-only** - it never changes `fit_score` or `verdict`. It shows up as a color-coded
`competition` column (green = open field, red = saturated), a filter, and a **"worth a call + open
lane"** summary card (high-fit ideas that are *not* crowded - your best shots). Results are cached in
memory, so each idea is researched once. Turn it off with `--no-web-search` (falls back to model
knowledge; also the automatic fallback if a search errors). Cost: web search is ~$10/1k searches, so a
seed run's high-fit set adds well under $1; incremental runs add pennies.

## A note on the Reddit source (the "GummySearch lesson")

Reddit fights bulk reads. The public `.json` API `403`s flagged/datacenter IPs, and
[pullpush.io](https://pullpush.io) (a no-auth Pushshift successor) `429`s hard from datacenter IPs
like GitHub's runners - a full query sweep gets rate-limited into the ground. So the tool reads each
subreddit's **RSS `/new` feed** instead: **one request per sub** (166 vs ~500), *fresh* (not a stale
archive), and it returns `200` from the same IPs where `.json` and pullpush fail. RSS has no
server-side query, so the tool keeps only posts with pay-signal language (`PAY_SIGNAL_KEYWORDS`)
before classifying. pullpush stays as a per-sub fallback. XML is parsed with `defusedxml` (untrusted
input). One source failing never kills a run.

## Tests

```bash
python test_ask_my_market.py     # or: pytest -q
```

No network, no API - covers scoring, thresholds, JSON parsing, boundary coercion, XSS/URL
safety, persistence merge/bump, and prefilter community-balance.

## Roadmap (later sessions)

Freelance-marketplace recurring-gig detection · competitor 1-2 star review mining · niche
forums / vertical Slack-Discord archives · X / Bluesky · embedding-based clustering of duplicate
pains · daily digest email of `worth_a_call` items.

If continuous monitoring of a public stream for a specific buyer validates in real founder
conversations, *that* is when this monitoring engine becomes the product. Built for the decision,
not the imagined product.
