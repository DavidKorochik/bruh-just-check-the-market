# bruh, just check the market

A personal, single-sitting pain-discovery instrument. Not a product.
It mines Reddit + Hacker News for one specific shape of opportunity:

> A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is the shape of an agentic product hiding in the open. Claude reads each
candidate through one opinionated founder filter and every result carries
*where those people gather*, so the output doubles as an outreach target list.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python ask_my_market.py                    # full run -> writes + opens report.html
python ask_my_market.py --dry-run          # fetch + prefilter only, zero API cost
python ask_my_market.py --sample --limit 20   # fast smoke test (tiny source subset)
python ask_my_market.py --no-open          # headless (don't open browser)
```

Output is a single self-contained `report.html`: one row per item, sorted by
`fit_score` descending, verdict color-coded, with a checkbox to hide `skip` rows.

## The pipeline

```
config (subreddits + queries)
  -> fetch  (Reddit + Hacker News)
  -> prefilter  (dedup by url, drop un-judgeable noise)
  -> Claude judgment layer  (classify each survivor)
  -> report.html  -> open in browser
```

Everything is one file: [`ask_my_market.py`](./ask_my_market.py). Edit the
`CONFIG` block at the top - subreddits, queries, and the `WEIGHTS` dict that
drives `fit_score`.

## How scoring works

Claude returns the *judgment* fields (`wtp_tier`, `judgment_on_stream`, ...).
`fit_score` and `verdict` are computed **in Python** from the `WEIGHTS` dict, so
the math is deterministic and you tune scoring in one place instead of editing a
prompt. `paying_a_human` is weighted hardest - money already moving to a human
is the strongest signal that an agent can take the job.

| verdict        | fit_score |
|----------------|-----------|
| `worth_a_call` | >= 65     |
| `watch`        | 40 - 64   |
| `skip`         | < 40      |

## A note on the Reddit source (the "GummySearch lesson", on day one)

Reddit's public `.json` endpoints now hard-block non-browser / flagged IPs with
a `403`. The tool tries the official endpoint first and **falls back to
[pullpush.io](https://pullpush.io)** (a no-auth Pushshift successor) when
blocked. pullpush is a *queryable archive* (its index currently lags ~1 year),
so Reddit results are older but still valid pain; Hacker News carries the fresh
signal. One source failing never kills a run.

## Roadmap (not built - later sessions)

Freelance-marketplace recurring-gig detection · competitor 1-2 star review
mining · niche forums / vertical Slack-Discord archives · X / Bluesky ·
cross-run persistence + frequency-over-time · daily digest of `worth_a_call` ·
embedding clustering of duplicate pains · per-source resilience.

If continuous monitoring of a public stream for a specific buyer validates in
real founder conversations, *that* is when the monitoring engine becomes the
product. Not before. This v1 is built for the decision, not the imagined product.
