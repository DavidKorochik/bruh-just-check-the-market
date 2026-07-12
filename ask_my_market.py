#!/usr/bin/env python3
"""
Ask My Market - a personal pain-discovery instrument.

Not a product. A one-shot scanner that mines Reddit + Hacker News for a single,
specific shape of opportunity:

    A human being paid, or actively paying, to apply repeated judgment
    to a stream of information.

That is the shape of an agentic product hiding in the open. The judgment layer
(Claude) is opinionated toward one operator's filter, not neutral. Every result
carries "where they gather" so the output doubles as an outreach target list.

Pipeline:  config -> fetch (Reddit + HN) -> prefilter -> Claude judgment -> report.html -> open

Run:
    export ANTHROPIC_API_KEY=...
    python ask_my_market.py                 # full run
    python ask_my_market.py --dry-run       # fetch only, no API cost, prints counts
    python ask_my_market.py --sample --limit 20   # fast smoke test
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import sys
import time
import webbrowser
from pathlib import Path

import requests

# =============================================================================
# CONFIG  -  edit freely, this is your instrument
# =============================================================================

# Subreddits where operators (not consumers) complain. Boring verticals welcome.
SUBREDDITS = ["accounting", "msp", "sysadmin", "smallbusiness", "ExperiencedDevs", "devops"]

# Reddit search queries. Chosen to catch PAY-signal and MANUAL-judgment language,
# not generic complaints. "hired a VA to", "paying someone to" = money already moving.
QUERIES = [
    "I wish there was",
    "why is there no",
    "how do you even deal with",
    "paying someone to",
    "hired a VA to",
    "spend hours every",
]

# Hacker News queries (developer / founder audience, fresh stream).
HN_QUERIES = ["how do you track", "tired of manually", "wish there was a tool"]

# --- Judgment model -------------------------------------------------------
# Fast + cheap classifier. Swap here to trade cost for depth.
MODEL = "claude-haiku-4-5-20251001"

# --- fit_score weights  -  TUNE THESE. Scoring is done in Python (not the model)
# so this dict is the single source of truth and the model never has to do math.
# Score starts at 0, adds each weight whose condition holds, caps at 100.
WEIGHTS = {
    "wtp_paying_a_human": 35,      # someone pays a PERSON/VA to do this by hand. Strongest signal.
    "wtp_paying_for_bad_tool": 20, # pays for a tool that fails them ("X can't do Y").
    "wtp_just_complaining": 0,     # venting, no money moving.
    "judgment_on_stream": 25,      # watching a feed + repeated judgment. THE edge.
    "solo_shippable": 15,          # one engineer can ship a paid v1 in evenings.
    "time_to_pay_weeks": 10,       # buyer pays in weeks (good), not quarters.
    "funded_team_ignores": 10,     # market too niche for a VC-backed team to bother.
    "firsthand_domain": 5,         # dev tooling / security / founder-ops / compliance / investing.
}

# verdict thresholds on fit_score
WORTH_A_CALL_AT = 65   # >= this  -> worth_a_call
WATCH_AT = 40          # >= this  -> watch, else skip

# --- Fetch / cost knobs ---------------------------------------------------
MAX_ITEMS = 80              # cap on how many survivors get classified (bounds API cost)
REDDIT_PAGE = 25            # results per reddit search/browse call
HN_PAGE = 25               # results per HN query call
REDDIT_SLEEP = 1.2         # polite pause between reddit calls (rate-limit respect)
CLASSIFY_WORKERS = 5       # parallel classification threads
BODY_TRUNC = 1500          # chars of body sent to the model
MIN_CHARS = 40             # drop items shorter than this (title+body) as un-judgeable noise
HTTP_TIMEOUT = 30
USER_AGENT = "ask-my-market/0.1 (personal customer-discovery tool; contact u/DavidKorochik)"

# =============================================================================
# Normalized item  -  every source emits this shape (drop-in source design)
# =============================================================================


def make_item(source_type, item_id, title, body, url, where, created_iso):
    return {
        "source_type": source_type,   # "reddit" | "hackernews"
        "id": item_id,                # stable unique id (permalink / objectID)
        "title": (title or "").strip(),
        "body": (body or "").strip(),
        "url": url,
        "where": where,               # subreddit or "Hacker News"
        "created": created_iso,       # ISO date string or ""
    }


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.UTC).date().isoformat()
    except Exception:
        return ""


# =============================================================================
# Fetchers  -  each returns list[item]; failures raise, fetch_all isolates them
# =============================================================================


def fetch_hn(queries):
    """Hacker News via Algolia. Zero auth, clean JSON, fresh. Stories + comments."""
    items = []
    base = "https://hn.algolia.com/api/v1/search_by_date"
    for q in queries:
        for tag in ("story", "comment"):
            try:
                r = requests.get(
                    base,
                    params={"query": q, "tags": tag, "hitsPerPage": HN_PAGE},
                    headers={"User-Agent": USER_AGENT},
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                hits = r.json().get("hits", [])
            except Exception as e:
                print(f"  [hn] '{q}' ({tag}) failed: {e}", file=sys.stderr)
                continue
            for h in hits:
                oid = h.get("objectID")
                if not oid:
                    continue
                url = f"https://news.ycombinator.com/item?id={oid}"
                if tag == "story":
                    title = h.get("title") or h.get("story_title") or ""
                    body = h.get("story_text") or ""
                else:  # comment
                    title = h.get("story_title") or ""
                    body = h.get("comment_text") or ""
                items.append(
                    make_item("hackernews", url, title, _strip_html(body), url,
                              "Hacker News", h.get("created_at", "")[:10])
                )
    return items


def _strip_html(text):
    """HN comment_text carries HTML entities/tags. Cheap unescape + tag drop."""
    if not text:
        return ""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def fetch_reddit(subreddits, queries):
    """
    Reddit. Tries the official public JSON first (spec's intended path); on a
    block (403) falls back to pullpush.io for the whole run. This is the
    source-resilience the roadmap calls for: one source failing never kills it.
    """
    use_official = _reddit_official_works()
    backend = "official reddit .json" if use_official else "pullpush.io archive"
    print(f"  [reddit] backend: {backend}")

    items = []
    for sub in subreddits:
        # one query-less browse (recent pain) + the pay-signal queries
        jobs = [None] + list(queries)
        for q in jobs:
            try:
                rows = (_reddit_official(sub, q) if use_official
                        else _reddit_pullpush(sub, q))
                items.extend(rows)
            except Exception as e:
                print(f"  [reddit] r/{sub} q={q!r} failed: {e}", file=sys.stderr)
            time.sleep(REDDIT_SLEEP)
    return items


def _reddit_official_works():
    """Probe once. Reddit blocks datacenter/flagged IPs with 403 + HTML page."""
    try:
        r = requests.get(
            "https://www.reddit.com/r/msp/new.json?limit=1",
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        return r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
    except Exception:
        return False


def _reddit_official(sub, query):
    if query is None:
        url = f"https://www.reddit.com/r/{sub}/new.json"
        params = {"limit": REDDIT_PAGE}
    else:
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": query, "restrict_sr": 1, "sort": "new", "limit": REDDIT_PAGE}
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for child in r.json().get("data", {}).get("children", []):
        out.append(_reddit_row(child.get("data", {})))
    return out


def _reddit_pullpush(sub, query):
    """pullpush.io - no-auth Pushshift successor. Query-searchable Reddit archive."""
    params = {"subreddit": sub, "size": REDDIT_PAGE, "sort": "desc", "sort_type": "created_utc"}
    if query is not None:
        params["q"] = query
    r = requests.get(
        "https://api.pullpush.io/reddit/search/submission/",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 429:  # rate-limited: one polite retry, then give up on this query
        time.sleep(3)
        r = requests.get("https://api.pullpush.io/reddit/search/submission/",
                         params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return [_reddit_row(d) for d in r.json().get("data", [])]


def _reddit_row(d):
    permalink = d.get("permalink", "")
    url = ("https://www.reddit.com" + permalink) if permalink else (d.get("url") or "")
    return make_item(
        "reddit",
        url or d.get("id", ""),
        d.get("title", ""),
        d.get("selftext", ""),
        url,
        "r/" + str(d.get("subreddit", "")),
        _iso(d.get("created_utc")),
    )


def fetch_all(sample=False):
    """Run every source, isolate failures. One dead source never kills the run."""
    subs = SUBREDDITS[:1] if sample else SUBREDDITS
    r_queries = QUERIES[:2] if sample else QUERIES
    hn_queries = HN_QUERIES[:1] if sample else HN_QUERIES

    items = []
    for name, fn in [("reddit", lambda: fetch_reddit(subs, r_queries)),
                     ("hackernews", lambda: fetch_hn(hn_queries))]:
        try:
            got = fn()
            print(f"  fetched {len(got)} from {name}")
            items.extend(got)
        except Exception as e:
            print(f"  [{name}] source failed entirely: {e}", file=sys.stderr)
    return items


# =============================================================================
# Prefilter  -  dedup + drop un-judgeable noise before spending tokens
# =============================================================================


def prefilter(items, limit):
    seen = set()
    by_source = {}
    for it in items:
        key = it["url"] or it["id"]
        if not key or key in seen:
            continue
        if len(it["title"]) + len(it["body"]) < MIN_CHARS:
            continue
        seen.add(key)
        by_source.setdefault(it["source_type"], []).append(it)
    # Round-robin across sources so the cap keeps a BALANCED mix. A pure
    # freshest-first sort would let HN (live) bury Reddit (archived) and starve
    # the pay-signal query matches, which are the whole point.
    queues = list(by_source.values())
    kept = []
    i = 0
    while queues and len(kept) < limit:
        q = queues[i % len(queues)]
        kept.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return kept


# =============================================================================
# Judgment layer  -  the heart. Claude classifies; Python scores.
# =============================================================================

SYSTEM_PROMPT = """You are the discovery co-founder for a solo founder-engineer who builds agentic \
investigation systems for a living. You share his exact, opinionated filter. You are NOT a neutral \
pain-finder. You are hunting one specific shape:

  A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is an agentic product hiding in the open. Be RUTHLESS. Most items are noise and must be skipped. \
If you are torn, you skip. A tool that flags everything is useless.

For the one item given, return ONLY a strict JSON object (no prose, no markdown fences) with EXACTLY \
these keys:

  "pain":               one sentence, the real problem in the person's own framing.
  "quote":              verbatim from the text, UNDER 15 words, the single sharpest line. "" if none.
  "wtp_tier":           one of:
                          "paying_a_human"       -> mentions hiring/paying a person/VA/freelancer/assistant
                                                    to do this by hand. STRONGEST signal.
                          "paying_for_bad_tool"  -> uses/pays for an existing tool that fails them ("X can't do Y").
                          "just_complaining"     -> venting, no evidence money moves.
  "judgment_on_stream": true/false. Does solving it mean watching a STREAM of information and applying
                        REPEATED judgment (monitoring, triaging, categorizing, flagging, summarizing a feed)?
  "solo_shippable":     true/false. Could ONE engineer ship a paid v1 in evenings, no team?
  "time_to_pay":        "weeks" | "quarters" | "unknown". Realistic time until a buyer pays.
  "funded_team_ignores":true/false. Is the market niche enough (a few million, boring) that a VC-backed
                        team would not bother? (For us that is GOOD.)
  "firsthand_domain":   true/false. Is it in: dev tooling, security/SOC/incident response, founder-ops,
                        regulatory/tax compliance, or investing?
  "where_they_gather":  concrete place to find these people (subreddit / forum / community). ALWAYS fill it.

Return the JSON object and nothing else."""

VALID_WTP = {"paying_a_human", "paying_for_bad_tool", "just_complaining"}
VALID_TTP = {"weeks", "quarters", "unknown"}


def compute_score(c):
    """Deterministic fit_score from classified fields. Single source of truth = WEIGHTS."""
    s = 0
    wtp = c.get("wtp_tier")
    if wtp == "paying_a_human":
        s += WEIGHTS["wtp_paying_a_human"]
    elif wtp == "paying_for_bad_tool":
        s += WEIGHTS["wtp_paying_for_bad_tool"]
    # just_complaining adds 0
    if c.get("judgment_on_stream"):
        s += WEIGHTS["judgment_on_stream"]
    if c.get("solo_shippable"):
        s += WEIGHTS["solo_shippable"]
    if c.get("time_to_pay") == "weeks":
        s += WEIGHTS["time_to_pay_weeks"]
    if c.get("funded_team_ignores"):
        s += WEIGHTS["funded_team_ignores"]
    if c.get("firsthand_domain"):
        s += WEIGHTS["firsthand_domain"]
    return min(s, 100)


def verdict_for(score):
    if score >= WORTH_A_CALL_AT:
        return "worth_a_call"
    if score >= WATCH_AT:
        return "watch"
    return "skip"


def _extract_json(text):
    """Model should return bare JSON, but strip fences / surrounding prose defensively."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def _coerce(c):
    """Validate at the boundary. Never trust the model's shape blindly."""
    if c.get("wtp_tier") not in VALID_WTP:
        c["wtp_tier"] = "just_complaining"
    if c.get("time_to_pay") not in VALID_TTP:
        c["time_to_pay"] = "unknown"
    for k in ("judgment_on_stream", "solo_shippable", "funded_team_ignores", "firsthand_domain"):
        c[k] = bool(c.get(k))
    c["pain"] = str(c.get("pain") or "").strip()
    c["quote"] = str(c.get("quote") or "").strip()
    c["where_they_gather"] = str(c.get("where_they_gather") or "").strip()
    return c


def classify_item(client, item):
    """Classify one item. Returns the enriched record, or None on failure (isolated)."""
    body = item["body"][:BODY_TRUNC]
    user = f"Title: {item['title']}\n\nBody: {body}\n\nSource: {item['where']}"
    for attempt in range(2):  # one retry on transient/parse error
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=500,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            c = _coerce(_extract_json(msg.content[0].text))
            break
        except Exception as e:
            if attempt == 1:
                print(f"  [classify] gave up on {item['url']}: {e}", file=sys.stderr)
                return None
            time.sleep(1)
    score = compute_score(c)
    c["fit_score"] = score
    c["verdict"] = verdict_for(score)
    c["source_url"] = item["url"]
    c["source_type"] = item["source_type"]
    c["where_they_gather"] = c["where_they_gather"] or item["where"]
    return c


def classify_all(items):
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    results = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = [pool.submit(classify_item, client, it) for it in items]
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"\r  classified {done}/{len(items)}", end="", flush=True)
    print()
    results.sort(key=lambda x: x["fit_score"], reverse=True)
    return results


# =============================================================================
# Report  -  one self-contained static HTML file, sorted, colored, filterable
# =============================================================================

VERDICT_COLOR = {"worth_a_call": "#1a7f37", "watch": "#b7791f", "skip": "#6b7280"}


def render_html(results, fetched_count, kept_count):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {"worth_a_call": 0, "watch": 0, "skip": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    rows = []
    for r in results:
        color = VERDICT_COLOR.get(r["verdict"], "#6b7280")
        link = html.escape(r["source_url"] or "")
        src = html.escape(r["source_type"])
        rows.append(f"""
      <tr class="v-{r['verdict']}">
        <td class="num">{r['fit_score']}</td>
        <td><span class="badge" style="background:{color}">{html.escape(r['verdict'])}</span></td>
        <td>{html.escape(r['wtp_tier'])}</td>
        <td>{html.escape(r['pain'])}</td>
        <td class="quote">{html.escape(r['quote'])}</td>
        <td>{html.escape(r['where_they_gather'])}</td>
        <td><a href="{link}" target="_blank" rel="noopener">{src} &#8599;</a></td>
      </tr>""")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ask My Market - {ts}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #0d1117; color: #e6edf3; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #9198a1; font-size: 13px; margin-bottom: 16px; }}
  .meta b {{ color: #e6edf3; }}
  .controls {{ margin: 12px 0; font-size: 14px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d; vertical-align: top; }}
  th {{ position: sticky; top: 0; background: #161b22; font-size: 12px; text-transform: uppercase;
        letter-spacing: .04em; color: #9198a1; }}
  td.num {{ font-weight: 700; font-variant-numeric: tabular-nums; }}
  td.quote {{ font-style: italic; color: #9198a1; max-width: 240px; }}
  .badge {{ color: #fff; padding: 2px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  tr:hover td {{ background: #161b22; }}
  @media (prefers-color-scheme: light) {{
    body {{ background: #fff; color: #1f2328; }}
    .meta, td.quote, th {{ color: #59636e; }}
    .meta b {{ color: #1f2328; }}
    th {{ background: #f6f8fa; }}
    th, td {{ border-bottom-color: #d1d9e0; }}
    tr:hover td {{ background: #f6f8fa; }}
  }}
</style></head>
<body>
  <h1>Ask My Market</h1>
  <div class="meta">
    Run <b>{ts}</b> &middot; fetched <b>{fetched_count}</b> &middot; classified <b>{kept_count}</b> &middot;
    <b style="color:{VERDICT_COLOR['worth_a_call']}">{counts['worth_a_call']} worth a call</b> &middot;
    <b style="color:{VERDICT_COLOR['watch']}">{counts['watch']} watch</b> &middot;
    <b>{counts['skip']} skip</b>
  </div>
  <div class="controls">
    <label><input type="checkbox" id="hideSkip" onchange="toggle()"> Hide <b>skip</b> rows</label>
  </div>
  <table>
    <thead><tr>
      <th>fit</th><th>verdict</th><th>wtp tier</th><th>pain</th>
      <th>sharpest quote</th><th>where they gather</th><th>source</th>
    </tr></thead>
    <tbody>{''.join(rows) if rows else '<tr><td colspan="7">No items survived. Try more subs/queries.</td></tr>'}</tbody>
  </table>
<script>
  function toggle() {{
    var hide = document.getElementById('hideSkip').checked;
    document.querySelectorAll('tr.v-skip').forEach(function(r) {{
      r.style.display = hide ? 'none' : '';
    }});
  }}
</script>
</body></html>"""


def write_and_open(results, fetched_count, kept_count, out_path, open_browser):
    out = Path(out_path)
    out.write_text(render_html(results, fetched_count, kept_count), encoding="utf-8")
    print(f"\nReport: {out.resolve()}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())


# =============================================================================
# Main
# =============================================================================


def main():
    ap = argparse.ArgumentParser(description="Personal pain-discovery scanner.")
    ap.add_argument("--limit", type=int, default=MAX_ITEMS, help="max items to classify")
    ap.add_argument("--sample", action="store_true", help="tiny source subset for a fast smoke test")
    ap.add_argument("--dry-run", action="store_true", help="fetch + prefilter only, no API cost")
    ap.add_argument("--no-open", action="store_true", help="do not open the report in a browser")
    args = ap.parse_args()

    print("Fetching...")
    raw = fetch_all(sample=args.sample)
    kept = prefilter(raw, args.limit)
    print(f"Prefiltered {len(raw)} -> {len(kept)} candidates (cap {args.limit}).")

    if args.dry_run:
        for it in kept[:15]:
            print(f"  [{it['source_type']}] {it['created']} {it['where']}: {it['title'][:70]}")
        print("\n(dry run: no classification, no report)")
        return

    if not kept:
        write_and_open([], len(raw), 0, "report.html", not args.no_open)
        return

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. export it and retry (or use --dry-run).")

    print(f"Classifying {len(kept)} items with {MODEL}...")
    results = classify_all(kept)
    write_and_open(results, len(raw), len(kept), "report.html", not args.no_open)


if __name__ == "__main__":
    main()
