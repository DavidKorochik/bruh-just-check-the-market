# bruh, just check the market

A personal, economy-wide pain-discovery instrument. Not a product.
Built on one observation: real user pain is **distributed** across many places
(subreddits, issue trackers, HN threads, Bluesky), so nobody sees it whole.
This tool gathers it into one structured, summarized place and reads it through
one opinionated filter:

> A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is the shape of an agentic product hiding in the open. The output is
**pain-first**: what hurts, who it hurts, how often it resurfaces, and where those
people gather - so every finding doubles as an outreach target, complete with a
**ready-to-post discovery reply**. Product direction is derived from the pain,
not the other way around. It runs itself every 2 days and publishes a filterable
dashboard to GitHub Pages - no live server, no running cost.

## What v3 adds

- **Pain-pattern synthesis.** Each scan ends with a CONCLUSION, not just rows: Claude clusters the
  accumulated high-fit findings into recurring pain patterns (the pain in the sufferers' own
  framing, who hurts, cross-source corroboration computed in Python, an optional product direction,
  and a concrete discovery next step). Rendered as cards at the top of the dashboard, and as a
  markdown digest on the Action run page.
- **Draft discovery replies.** Every high-fit finding gets a short, honest reply you can post on
  the source thread to open a customer-discovery conversation - references a concrete detail from
  the post, asks exactly one workflow/frequency/cost question, never pitches, never poses as a
  fellow sufferer.
- **Two new sources.** GitHub issue search ("our tool can't do X, we do it by hand" attached to the
  failing product itself - the purest paying-for-bad-tool evidence) and Bluesky public search
  (in-the-moment operator complaints). Both free, no auth required.

## What v2 added

- **Whole economy, not just tech.** [`industries.py`](./industries.py) is a taxonomy of
  **36 GDP sectors / 166 operator subreddits** - agriculture, trucking, dental, legal,
  oil & gas, funeral homes, customs compliance, and on. Every finding is tagged with an `industry`.
- **Memory.** [`data/findings.json`](./data) accumulates across runs (dedup by URL) and tracks
  `times_seen` - a pain that keeps resurfacing is a stronger signal. The dashboard grows into a
  standing board instead of a throwaway snapshot.
- **Hosted dashboard.** A GitHub Action runs the scan every 2 days and deploys a filterable
  dashboard (summary cards, live filters by verdict / industry / wtp-tier / source,
  full-text search) to GitHub Pages.

## The pipeline

```
load memory -> fetch (Reddit + HN + GitHub issues + Bluesky) -> prefilter -> skip already-seen
  -> Claude judgment (new items only) -> competition check + discovery replies (high-fit only)
  -> merge into memory -> pain-pattern synthesis -> dashboard + digest
```

Four files do the work: [`ask_my_market.py`](./ask_my_market.py) (pipeline + judgment),
[`sources.py`](./sources.py) (fetchers), [`report.py`](./report.py) (dashboard + digest), and
[`industries.py`](./industries.py) (the sector/subreddit/query taxonomy - edit freely).

Model split: classification runs on **Haiku** (high-volume rubric work); competition, discovery
replies, and pattern synthesis run on **Sonnet** (low-volume judgment the output is acted on).

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python ask_my_market.py                 # incremental run -> writes + opens report.html
python ask_my_market.py --dry-run       # fetch + prefilter only, zero API cost
python ask_my_market.py --sample --limit 20   # fast smoke test (tiny source subset)
```

`ANTHROPIC_API_KEY` must be a **Console pay-as-you-go key** (`sk-ant-api03-...` from
<https://console.anthropic.com>), not a Claude Max/Pro subscription - the subscription has no
Messages-API key. Per full run: Haiku classification (~$0.45 at the 200-item cap) + Sonnet
competition/replies/synthesis for the high-fit slice (~$0.40) - still under $1; incremental
runs are pennies.

Useful flags: `--limit N` (max NEW items classified), `--data PATH` (memory file),
`--out PATH` (dashboard path), `--no-open`, `--no-outreach` (skip discovery replies),
`--no-patterns` (skip the synthesis). Optional: `GITHUB_TOKEN` triples the GitHub
issue-search rate limit (automatic in Actions).

## Host it on GitHub Pages (zero running cost)

One-time setup:

1. **Add your Anthropic API key as a secret.** It must be a **Console pay-as-you-go API key** from
   <https://console.anthropic.com> (format `sk-ant-api03-...`), **not** a Claude Max/Pro subscription
   credential - the subscription does not include an API key, and the Messages API rejects anything
   else with `401 invalid x-api-key` (which aborts the run):
   ```bash
   gh secret set ANTHROPIC_API_KEY --repo DavidKorochik/bruh-just-check-the-market
   ```
2. **Add Reddit OAuth creds** (required in CI - reddit blocks anonymous reads from GitHub's IPs).
   Create a free app at <https://www.reddit.com/prefs/apps> → "create app" → type **script** →
   redirect URI `http://localhost`. The client id is under the app name; the secret is labeled "secret":
   ```bash
   gh secret set REDDIT_CLIENT_ID     --repo DavidKorochik/bruh-just-check-the-market
   gh secret set REDDIT_CLIENT_SECRET --repo DavidKorochik/bruh-just-check-the-market
   ```
3. **Turn on Pages -> GitHub Actions** (Settings -> Pages -> Source: GitHub Actions), or:
   ```bash
   gh api -X POST repos/DavidKorochik/bruh-just-check-the-market/pages -f build_type=workflow
   ```
4. **Kick off the first run** (~a few minutes):
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

Reddit fights bulk reads, and it blocks **GitHub's runner IPs on every anonymous endpoint** -
`.json` `403`s, RSS `429`s, pullpush `429`s. So the hosted scan uses the **authenticated OAuth API**
(`oauth.reddit.com`), which is rate-limited per *account*, not per IP, and works from anywhere. The
tool **auto-selects** its backend:

- **OAuth** when `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are set (CI) - restores server-side pay-signal query search.
- **RSS `/new`** when they're not (a local run from a residential IP, which reddit doesn't block) - unfiltered recent, filtered locally by `PAY_SIGNAL_KEYWORDS`.
- **pullpush.io** as a last-ditch per-sub fallback.

XML (RSS) is parsed with `defusedxml` (untrusted input). One source failing never kills a run.

## Tests

```bash
python test_ask_my_market.py     # or: pytest -q
```

No network, no API - covers scoring, thresholds, JSON parsing, boundary coercion, XSS/URL
safety, persistence merge/bump, prefilter community-balance, pattern-synthesis coercion,
source parsing (RSS/Bluesky/GitHub), and end-to-end `main()` failure guards (a dead API key
or a wholesale classify failure exits nonzero and never re-deploys stale data).

## Roadmap (later sessions)

Freelance-marketplace recurring-gig detection (job posts ARE "paying a human" evidence) ·
competitor 1-2 star review mining · app-store review mining (iTunes RSS is public JSON) ·
niche Discourse forums (every instance exposes `/search.json`) · vertical Slack-Discord
archives · embedding-based clustering of duplicate pains · daily digest email of
`worth_a_call` items · alerting when a Reddit OAuth credential silently expires.

If continuous monitoring of a public stream for a specific buyer validates in real founder
conversations, *that* is when this monitoring engine becomes the product. Built for the decision,
not the imagined product.
