#!/usr/bin/env python3
"""
Ask My Market - a personal pain-discovery instrument (v2).

Not a product. A scanner that mines Reddit + Hacker News, ACROSS THE WHOLE
ECONOMY, for one specific shape of opportunity:

    A human being paid, or actively paying, to apply repeated judgment
    to a stream of information.

That is the shape of an agentic product hiding in the open. The judgment layer
(Claude) is opinionated toward one operator's filter. Every result carries the
industry and "where they gather", so the output doubles as an outreach list.

v2 adds: economy-wide industry taxonomy (industries.py), a `industry` tag on
every finding, cross-run memory (data/findings.json) so the dashboard ACCUMULATES
and tracks frequency-over-time, and a redesigned filterable dashboard suitable
for hosting on GitHub Pages via a scheduled Action.

Pipeline:
    load memory -> fetch (Reddit + HN) -> prefilter -> skip already-seen
    -> Claude judgment (new only) -> merge into memory -> render dashboard -> open

Run:
    export ANTHROPIC_API_KEY=...
    python ask_my_market.py                     # incremental run
    python ask_my_market.py --reseed            # re-pull the frozen Reddit archive
    python ask_my_market.py --dry-run --sample  # fetch only, no API cost
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import requests

from industries import BASE_QUERIES, HN_QUERIES, INDUSTRIES

# =============================================================================
# CONFIG  -  edit freely (industries.py holds the sector/subreddit/query taxonomy)
# =============================================================================

# Fast + cheap classifier. Swap here to trade cost for depth.
MODEL = "claude-haiku-4-5-20251001"

# --- fit_score weights  -  TUNE THESE. Scoring is done in Python (not the model)
# so this dict is the single source of truth and the model never does math.
WEIGHTS = {
    "wtp_paying_a_human": 35,       # pays a PERSON/VA to do it by hand. Strongest signal.
    "wtp_paying_for_bad_tool": 20,  # pays for a tool that fails them ("X can't do Y").
    "wtp_just_complaining": 0,      # venting, no money moving.
    "judgment_on_stream": 25,       # watching a feed + repeated judgment. THE edge.
    "solo_shippable": 15,           # one engineer can ship a paid v1 in evenings.
    "time_to_pay_weeks": 10,        # buyer pays in weeks (good), not quarters.
    "funded_team_ignores": 10,      # market too niche for a VC-backed team to bother.
    "firsthand_domain": 5,          # dev tooling / security / founder-ops / compliance / investing.
}

WORTH_A_CALL_AT = 65   # fit_score >= this -> worth_a_call
WATCH_AT = 40          # >= this -> watch, else skip

# --- Fetch / cost knobs ---------------------------------------------------
MAX_ITEMS = 200            # cap on how many NEW survivors get classified per run
QUERIES_PER_SUB = 3        # pay-signal queries fired per subreddit (bounds pullpush volume:
                           # 166 subs x this many calls; keep modest or the seed run crawls)
REDDIT_PAGE = 25
HN_PAGE = 25
REDDIT_SLEEP = 1.0        # polite pause between reddit calls
CLASSIFY_WORKERS = 5
BODY_TRUNC = 1500
MIN_CHARS = 40
HTTP_TIMEOUT = 30
USER_AGENT = "ask-my-market/0.2 (personal customer-discovery tool; contact u/DavidKorochik)"

DATA_PATH = "data/findings.json"   # cross-run memory (committed by the Action)

# Short industry labels the model is nudged toward (free-text, but consistent).
SECTOR_HINTS = sorted({i["sector"] for i in INDUSTRIES})

# =============================================================================
# Normalized item  -  every source emits this shape (drop-in source design)
# =============================================================================


def make_item(source_type, item_id, title, body, url, where, created_iso):
    return {
        "source_type": source_type,   # "reddit" | "hackernews"
        "id": item_id,
        "title": (title or "").strip(),
        "body": (body or "").strip(),
        "url": url,
        "where": where,               # subreddit or "Hacker News"
        "created": created_iso,
    }


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).date().isoformat()
    except Exception:
        return ""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# =============================================================================
# Fetchers  -  each returns list[item]; failures are isolated so one dead
# source never kills a run.
# =============================================================================


def fetch_hn(queries):
    """Hacker News via Algolia. Zero auth, clean JSON, fresh. Stories + comments."""
    items = []
    base = "https://hn.algolia.com/api/v1/search_by_date"
    for q in queries:
        for tag in ("story", "comment"):
            try:
                r = requests.get(base, params={"query": q, "tags": tag, "hitsPerPage": HN_PAGE},
                                 headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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
                    title, body = (h.get("title") or h.get("story_title") or ""), (h.get("story_text") or "")
                else:
                    title, body = (h.get("story_title") or ""), (h.get("comment_text") or "")
                items.append(make_item("hackernews", url, title, _strip_html(body), url,
                                       "Hacker News", h.get("created_at", "")[:10]))
    return items


def _strip_html(text):
    if not text:
        return ""
    import re
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def fetch_reddit(industries, use_official, sample=False):
    """Reddit across the whole industry taxonomy. Official public JSON first
    (fresh, works from un-blocked IPs), else pullpush.io archive fallback."""
    sectors = industries[:2] if sample else industries
    items = []
    for sec in sectors:
        subs = sec["subreddits"][:1] if sample else sec["subreddits"]
        # Per sub: a few TARGETED pay-signal queries (sector-specific first, then universal),
        # bounded by QUERIES_PER_SUB - 166 subs x many queries would be thousands of
        # rate-limited pullpush calls, and we only classify MAX_ITEMS anyway.
        n = 2 if sample else QUERIES_PER_SUB
        queries = (sec.get("queries", [])[:1] + BASE_QUERIES)[:n]
        for sub in subs:
            name = sub["name"]
            for q in queries:
                try:
                    rows = (_reddit_official(name, q) if use_official else _reddit_pullpush(name, q))
                    items.extend(rows)
                except Exception as e:
                    print(f"  [reddit] r/{name} q={q!r} failed: {e}", file=sys.stderr)
                time.sleep(REDDIT_SLEEP)
    return items


def _reddit_official_works(sub):
    """Probe once against a REAL configured sub. Reddit blocks flagged IPs with 403."""
    try:
        r = requests.get(f"https://www.reddit.com/r/{sub}/new.json?limit=1",
                         headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        return r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
    except Exception:
        return False


def _reddit_official(sub, query):
    if query is None:
        url, params = f"https://www.reddit.com/r/{sub}/new.json", {"limit": REDDIT_PAGE}
    else:
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": query, "restrict_sr": 1, "sort": "new", "limit": REDDIT_PAGE}
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return [_reddit_row(c.get("data", {})) for c in r.json().get("data", {}).get("children", [])]


def _reddit_pullpush(sub, query):
    """pullpush.io - no-auth Pushshift successor. Rate-limits hard; back off and
    retry so a transient 429 doesn't silently drop a whole (valuable) query."""
    params = {"subreddit": sub, "size": REDDIT_PAGE, "sort": "desc", "sort_type": "created_utc"}
    if query is not None:
        params["q"] = query
    r = None
    for delay in (0, 2, 5, 10):  # first try, then escalating backoff on 429
        if delay:
            time.sleep(delay)
        r = requests.get("https://api.pullpush.io/reddit/search/submission/",
                         params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if r.status_code != 429:
            break
    r.raise_for_status()
    return [_reddit_row(d) for d in r.json().get("data", [])]


def _reddit_row(d):
    permalink = d.get("permalink", "")
    url = ("https://www.reddit.com" + permalink) if permalink else (d.get("url") or "")
    return make_item("reddit", url or d.get("id", ""), d.get("title", ""), d.get("selftext", ""),
                     url, "r/" + str(d.get("subreddit", "")), _iso(d.get("created_utc")))


# =============================================================================
# Prefilter  -  dedup + drop un-judgeable noise, balanced across sources
# =============================================================================


def prefilter(items, limit):
    seen = set()
    by_bucket = {}
    for it in items:
        key = it["url"] or it["id"]
        if not key or key in seen:
            continue
        if len(it["title"]) + len(it["body"]) < MIN_CHARS:
            continue
        seen.add(key)
        # Round-robin by COMMUNITY (subreddit / "Hacker News"), not by source, so the
        # MAX_ITEMS cap spreads across INDUSTRIES instead of letting a few big subs
        # (or fresh HN) dominate. HN is one bucket -> a minority on the reddit-heavy
        # seed run, and the sole source on incremental runs (reddit skipped once seeded).
        by_bucket.setdefault(it["where"], []).append(it)
    queues = list(by_bucket.values())
    kept, i = [], 0
    while queues and len(kept) < limit:
        q = queues[i % len(queues)]
        kept.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return kept


# =============================================================================
# Judgment layer  -  Claude classifies; Python scores.
# =============================================================================

SYSTEM_PROMPT = f"""You are the discovery co-founder for a solo founder-engineer who builds agentic \
investigation systems for a living. You share his exact, opinionated filter. You are NOT a neutral \
pain-finder. You hunt one specific shape, ACROSS ANY INDUSTRY (not just tech):

  A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is an agentic product hiding in the open. Be RUTHLESS. Most items are noise and must be skipped. \
If you are torn, you skip. A tool that flags everything is useless.

For the one item given, return ONLY a strict JSON object (no prose, no markdown fences) with EXACTLY \
these keys:

  "pain":               one sentence, the real problem in the person's own framing.
  "quote":              verbatim from the text, UNDER 15 words, the single sharpest line. "" if none.
  "industry":           a short 1-3 word lowercase label for the sector this pain lives in
                        (e.g. accounting, trucking, healthcare, legal, real estate, construction,
                        restaurants, insurance, recruiting, dev tooling, msp). Known sectors include:
                        {", ".join(SECTOR_HINTS)}. Pick the closest, or coin a short label.
  "wtp_tier":           one of:
                          "paying_a_human"       -> hires/pays a person/VA/freelancer/assistant to do it by hand. STRONGEST.
                          "paying_for_bad_tool"  -> uses/pays for an existing tool that fails them ("X can't do Y").
                          "just_complaining"     -> venting, no evidence money moves.
  "judgment_on_stream": true/false. Does solving it mean watching a STREAM of information and applying
                        REPEATED judgment (monitoring, triaging, categorizing, reconciling, flagging, summarizing)?
  "solo_shippable":     true/false. Could ONE engineer ship a paid v1 in evenings, no team?
  "time_to_pay":        "weeks" | "quarters" | "unknown".
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
    c["industry"] = (str(c.get("industry") or "other").strip().lower() or "other")[:30]
    return c


def classify_item(client, item):
    """Classify one item. Returns the enriched record, or None on failure (isolated)."""
    body = item["body"][:BODY_TRUNC]
    user = f"Title: {item['title']}\n\nBody: {body}\n\nSource: {item['where']}"
    for attempt in range(2):
        try:
            msg = client.messages.create(model=MODEL, max_tokens=500, temperature=0,
                                         system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user}])
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
    client = anthropic.Anthropic()
    results, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = [pool.submit(classify_item, client, it) for it in items]
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"\r  classified {done}/{len(items)}", end="", flush=True)
    print()
    return results


# =============================================================================
# Persistence  -  cross-run memory so the dashboard ACCUMULATES + tracks frequency
# =============================================================================


def load_store(path):
    """Returns {'_meta': {...}, 'findings': {url: record}}. Tolerates a missing/corrupt file."""
    p = Path(path)
    if not p.exists():
        return {"_meta": {}, "findings": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("_meta", {})
        data.setdefault("findings", {})
        return data
    except Exception as e:
        print(f"  [store] {path} unreadable ({e}); starting fresh", file=sys.stderr)
        return {"_meta": {}, "findings": {}}


def merge_new(store, records, now):
    """Insert freshly-classified records with first_seen/last_seen/times_seen=1."""
    for r in records:
        r["first_seen"] = now
        r["last_seen"] = now
        r["times_seen"] = 1
        store["findings"][r["source_url"]] = r


def bump_seen(store, urls, now):
    """A URL surfaced again this run -> stronger frequency signal."""
    for u in urls:
        rec = store["findings"].get(u)
        if rec:
            rec["last_seen"] = now
            rec["times_seen"] = rec.get("times_seen", 1) + 1


def save_store(path, store):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")


# =============================================================================
# Report  -  one self-contained dashboard: summary cards + live filters + search
# =============================================================================

VERDICT_COLOR = {"worth_a_call": "#1a7f37", "watch": "#b7791f", "skip": "#6b7280"}


def _safe_href(url):
    """Allow ONLY http(s). Blocks javascript:/data: scheme injection from scraped URLs."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def render_html(store, run_stats):
    findings = sorted(store["findings"].values(), key=lambda r: r.get("fit_score", 0), reverse=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {"worth_a_call": 0, "watch": 0, "skip": 0}
    industries = {}
    for r in findings:
        counts[r.get("verdict", "skip")] = counts.get(r.get("verdict", "skip"), 0) + 1
        industries[r.get("industry", "other")] = industries.get(r.get("industry", "other"), 0) + 1
    new_urls = run_stats.get("new_urls", set())

    ind_options = "".join(f'<option value="{html.escape(k)}">{html.escape(k)} ({v})</option>'
                          for k, v in sorted(industries.items(), key=lambda kv: -kv[1]))

    rows = []
    for r in findings:
        v = r.get("verdict", "skip")
        color = VERDICT_COLOR.get(v, "#6b7280")
        href = _safe_href(r.get("source_url", ""))
        src = html.escape(r.get("source_type", ""))
        src_cell = (f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{src} &#8599;</a>'
                    if href else src)
        is_new = r.get("source_url") in new_urls
        newbadge = '<span class="new">NEW</span>' if is_new else ""
        seen = r.get("times_seen", 1)
        search_blob = html.escape(f"{r.get('pain','')} {r.get('quote','')} {r.get('where_they_gather','')} {r.get('industry','')}".lower())
        rows.append(f"""
      <tr data-verdict="{v}" data-industry="{html.escape(r.get('industry','other'))}" data-wtp="{html.escape(r.get('wtp_tier',''))}"
          data-source="{src}" data-new="{'1' if is_new else '0'}" data-search="{search_blob}">
        <td class="num">{r.get('fit_score',0)}{newbadge}</td>
        <td><span class="badge" style="background:{color}">{html.escape(v)}</span></td>
        <td>{html.escape(r.get('industry','other'))}</td>
        <td>{html.escape(r.get('wtp_tier',''))}</td>
        <td>{html.escape(r.get('pain',''))}</td>
        <td class="quote">{html.escape(r.get('quote',''))}</td>
        <td>{html.escape(r.get('where_they_gather',''))}</td>
        <td class="num" title="times this pain resurfaced across runs">{seen}</td>
        <td>{src_cell}</td>
      </tr>""")

    def card(n, label, color=None):
        style = f' style="color:{color}"' if color else ""
        return f'<div class="card"><div class="n"{style}>{n}</div><div class="l">{label}</div></div>'

    cards = "".join([
        card(len(findings), "total findings"),
        card(counts["worth_a_call"], "worth a call", VERDICT_COLOR["worth_a_call"]),
        card(counts["watch"], "watch", VERDICT_COLOR["watch"]),
        card(run_stats.get("new_count", 0), "new this run", "#8957e5"),
        card(len(industries), "industries covered"),
    ])

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ask My Market</title>
<style>
  :root {{ color-scheme: light dark;
    --bg:#0d1117; --fg:#e6edf3; --dim:#9198a1; --line:#21262d; --panel:#161b22; --accent:#58a6ff; }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#fff; --fg:#1f2328; --dim:#59636e; --line:#d1d9e0; --panel:#f6f8fa; --accent:#0969da; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg); }}
  h1 {{ font-size: 22px; margin: 0 0 2px; letter-spacing: -.02em; }}
  .sub {{ color: var(--dim); font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 18px; min-width: 120px; }}
  .card .n {{ font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .card .l {{ color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }}
  select, input[type=search] {{ background: var(--panel); color: var(--fg); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 10px; font-size: 14px; }}
  input[type=search] {{ min-width: 240px; flex: 1; }}
  label.chk {{ color: var(--dim); font-size: 14px; }}
  .count {{ color: var(--dim); font-size: 13px; margin-left: auto; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ position: sticky; top: 0; background: var(--panel); font-size: 11px; text-transform: uppercase;
    letter-spacing: .04em; color: var(--dim); cursor: default; z-index: 1; }}
  td.num {{ font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.quote {{ font-style: italic; color: var(--dim); max-width: 260px; }}
  .badge {{ color: #fff; padding: 2px 9px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
  .new {{ background: #8957e5; color: #fff; font-size: 9px; font-weight: 700; padding: 1px 5px;
    border-radius: 4px; margin-left: 6px; vertical-align: middle; }}
  a {{ color: var(--accent); text-decoration: none; }}
  tr:hover td {{ background: var(--panel); }}
</style></head>
<body>
  <h1>Ask My Market</h1>
  <div class="sub">Run {ts} &middot; {run_stats.get('fetched',0)} fetched &middot;
    {run_stats.get('new_count',0)} newly classified &middot; {len(findings)} total in memory</div>

  <div class="cards">{cards}</div>

  <div class="controls">
    <select id="fVerdict"><option value="">all verdicts</option>
      <option value="worth_a_call">worth a call</option><option value="watch">watch</option><option value="skip">skip</option></select>
    <select id="fIndustry"><option value="">all industries</option>{ind_options}</select>
    <select id="fWtp"><option value="">all wtp tiers</option>
      <option value="paying_a_human">paying a human</option><option value="paying_for_bad_tool">paying for bad tool</option>
      <option value="just_complaining">just complaining</option></select>
    <select id="fSource"><option value="">all sources</option>
      <option value="reddit">reddit</option><option value="hackernews">hackernews</option></select>
    <input type="search" id="fSearch" placeholder="search pain / quote / where / industry...">
    <label class="chk"><input type="checkbox" id="fNew"> new only</label>
    <span class="count" id="count"></span>
  </div>

  <table>
    <thead><tr>
      <th>fit</th><th>verdict</th><th>industry</th><th>wtp tier</th><th>pain</th>
      <th>sharpest quote</th><th>where they gather</th><th>seen</th><th>source</th>
    </tr></thead>
    <tbody id="tb">{''.join(rows) if rows else '<tr><td colspan="9">No findings yet. Run a scan.</td></tr>'}</tbody>
  </table>
<script>
  var rows = Array.prototype.slice.call(document.querySelectorAll('#tb tr[data-verdict]'));
  var ctl = {{v:'fVerdict', i:'fIndustry', w:'fWtp', s:'fSource', q:'fSearch', n:'fNew'}};
  Object.keys(ctl).forEach(function(k){{ var el=document.getElementById(ctl[k]); if(el) el.addEventListener('input', apply); }});
  function apply() {{
    var v=val('fVerdict'), i=val('fIndustry'), w=val('fWtp'), s=val('fSource'),
        q=val('fSearch').toLowerCase(), n=document.getElementById('fNew').checked;
    var shown=0;
    rows.forEach(function(r){{
      var ok = (!v||r.dataset.verdict===v) && (!i||r.dataset.industry===i) && (!w||r.dataset.wtp===w)
            && (!s||r.dataset.source===s) && (!n||r.dataset.new==='1') && (!q||r.dataset.search.indexOf(q)>-1);
      r.style.display = ok ? '' : 'none';
      if(ok) shown++;
    }});
    document.getElementById('count').textContent = shown + ' / ' + rows.length + ' shown';
  }}
  function val(id){{ return document.getElementById(id).value; }}
  apply();
</script>
</body></html>"""


def write_and_open(store, run_stats, out_path, open_browser):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(store, run_stats), encoding="utf-8")
    print(f"Dashboard: {out.resolve()}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())


# =============================================================================
# Main
# =============================================================================


def main():
    ap = argparse.ArgumentParser(description="Personal economy-wide pain-discovery scanner.")
    ap.add_argument("--limit", type=int, default=MAX_ITEMS, help="max NEW items to classify this run")
    ap.add_argument("--sample", action="store_true", help="tiny source subset for a fast smoke test")
    ap.add_argument("--dry-run", action="store_true", help="fetch + prefilter only, no API cost")
    ap.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")
    ap.add_argument("--reseed", action="store_true", help="re-fetch the frozen Reddit archive even if already seeded")
    ap.add_argument("--out", default="report.html", help="dashboard output path")
    ap.add_argument("--data", default=DATA_PATH, help="cross-run memory JSON path")
    args = ap.parse_args()

    store = load_store(args.data)
    already_seeded = bool(store["_meta"].get("reddit_seeded"))

    print("Probing Reddit backend...")
    first_sub = INDUSTRIES[0]["subreddits"][0]["name"] if INDUSTRIES else "msp"
    use_official = _reddit_official_works(first_sub)
    print(f"  reddit backend: {'official .json (fresh)' if use_official else 'pullpush.io archive (frozen)'}")

    # Skip re-pulling the FROZEN archive on scheduled incremental runs (it returns
    # identical data + hammers a rate-limited API). Fresh official backend never skips.
    skip_reddit = already_seeded and (not use_official) and (not args.reseed)

    print("Fetching...")
    raw = []
    try:
        hn = fetch_hn(HN_QUERIES[:1] if args.sample else HN_QUERIES)
        print(f"  fetched {len(hn)} from hackernews")
        raw.extend(hn)
    except Exception as e:
        print(f"  [hackernews] source failed entirely: {e}", file=sys.stderr)
    if skip_reddit:
        print("  [reddit] archive already seeded (frozen); skipping. Use --reseed to refetch.")
    else:
        try:
            rd = fetch_reddit(INDUSTRIES, use_official, sample=args.sample)
            print(f"  fetched {len(rd)} from reddit")
            raw.extend(rd)
            # Only mark the frozen archive "seeded" once we ACTUALLY got data - a
            # transient total-failure must not permanently skip Reddit on later runs.
            if not use_official and rd:
                store["_meta"]["reddit_seeded"] = True
        except Exception as e:
            print(f"  [reddit] source failed entirely: {e}", file=sys.stderr)

    kept = prefilter(raw, args.limit + 500)  # prefilter generously; split new/seen next
    # split against memory: classify only URLs we've never scored; bump the rest
    new_items = [it for it in kept if it["url"] not in store["findings"]][:args.limit]
    seen_again = [it["url"] for it in kept if it["url"] in store["findings"]]
    print(f"Prefiltered {len(raw)} -> {len(kept)} candidates; {len(new_items)} new, {len(seen_again)} already known.")

    if args.dry_run:
        for it in new_items[:15]:
            print(f"  [{it['source_type']}] {it['created']} {it['where']}: {it['title'][:70]}")
        print("\n(dry run: no classification, no dashboard)")
        return

    now = _now()
    bump_seen(store, seen_again, now)

    results = []
    if new_items:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY not set. export it and retry (or use --dry-run).")
        print(f"Classifying {len(new_items)} new items with {MODEL}...")
        results = classify_all(new_items)
        merge_new(store, results, now)
    else:
        print("No new items to classify.")

    store["_meta"]["last_run"] = now
    save_store(args.data, store)
    run_stats = {"fetched": len(raw), "new_count": len(results),
                 "new_urls": {r["source_url"] for r in results}}
    write_and_open(store, run_stats, args.out, not args.no_open)
    print(f"Memory: {Path(args.data).resolve()} ({len(store['findings'])} findings)")


if __name__ == "__main__":
    main()
