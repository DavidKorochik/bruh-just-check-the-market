#!/usr/bin/env python3
"""
Ask My Market - a personal pain-discovery instrument (v3).

Not a product. A scanner built on one observation: real user pain is DISTRIBUTED
across many places (subreddits, issue trackers, HN threads, Bluesky), so nobody
sees it whole. This tool gathers it into ONE structured, summarized place and
reads it through one opinionated filter:

    A human being paid, or actively paying, to apply repeated judgment
    to a stream of information.

That is the shape of an agentic product hiding in the open. The output is
pain-first: what hurts, who it hurts, how often it resurfaces, and where those
people gather - so every finding doubles as an outreach target, complete with a
ready-to-post discovery reply. Product direction is derived from the pain, not
the other way around.

v3 adds: pain-pattern synthesis (each scan ENDS WITH A CONCLUSION - recurring
pain themes across sources, with optional product direction), draft discovery
replies per high-fit finding, and two new sources (GitHub issues, Bluesky).

Pipeline:
    load memory -> fetch (Reddit + HN + GitHub + Bluesky) -> prefilter -> skip already-seen
    -> Claude judgment (new only) -> competition web-search + outreach drafts (high-fit)
    -> merge -> pain-pattern synthesis -> dashboard + digest

Run:
    export ANTHROPIC_API_KEY=...                # Console pay-as-you-go key (sk-ant-api03-...)
    python ask_my_market.py                     # scan -> classify new -> synthesize -> dashboard
    python ask_my_market.py --dry-run --sample  # fetch only, no API cost
    python ask_my_market.py --no-web-search     # skip the live competition web search
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from industries import BLUESKY_QUERIES, GITHUB_QUERIES, HN_QUERIES, INDUSTRIES
from report import write_and_open
from sources import fetch_bluesky, fetch_github, fetch_hn, fetch_reddit, make_item   # noqa: F401  (make_item: convenience re-export for the test suite)

# =============================================================================
# CONFIG  -  edit freely (industries.py holds the sector/subreddit/query taxonomy)
# =============================================================================

# Model split by job profile:
#   - classification: high-volume (up to 200/run), rubric-driven extraction -> Haiku (fast+cheap)
#   - competition / outreach / synthesis: low-volume, judgment-heavy, the outputs acted on -> Sonnet
# NOTE: "claude-sonnet-5" is an alias with NO dated snapshot (verified against the models API
# 2026-07-15) - scheduled runs may pick up model updates. Acceptable for a personal instrument.
MODEL = "claude-haiku-4-5-20251001"
COMPETITION_MODEL = "claude-sonnet-5"
OUTREACH_MODEL = "claude-sonnet-5"
SYNTH_MODEL = "claude-sonnet-5"

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
# Sonnet 5 thinks by default and can spend the ENTIRE max_tokens budget inside a thinking block,
# returning zero text (observed live: stop_reason=max_tokens, content=[thinking]). These calls all
# need strict JSON, so thinking is explicitly disabled. (It also rejects the temperature param.)
THINKING_OFF = {"type": "disabled"}
HIGH_FIT_VERDICTS = ("worth_a_call", "watch")  # competition + outreach only for these (skips ignored)
COMPETITION_VERDICTS = HIGH_FIT_VERDICTS       # back-compat alias
COMPETITION_WORKERS = 4
OUTREACH_WORKERS = 4

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

# --- Volume / cost knobs ----------------------------------------------------
MAX_ITEMS = 200            # cap on how many NEW survivors get classified per run
MIN_FATAL_BATCH = 5        # if a batch of >= this many NEW items classifies to ZERO survivors, the
                           # pipeline is broken (model down / network) -> exit nonzero instead of
                           # deploying nothing. Smaller all-fail batches are likely bad luck, so pass.
PATTERNS_MAX = 6           # pain patterns per synthesis
PATTERNS_INPUT_MAX = 40    # top findings fed to the synthesis call
CLASSIFY_WORKERS = 5
BODY_TRUNC = 1500
MIN_CHARS = 40

DATA_PATH = "data/findings.json"   # cross-run memory (committed by the Action)

# Short industry labels the model is nudged toward (free-text, but consistent).
SECTOR_HINTS = sorted({i["sector"] for i in INDUSTRIES})

# =============================================================================
# Prefilter  -  dedup + drop un-judgeable noise, balanced across communities
# =============================================================================


def prefilter(items, limit):
    seen = set()
    by_bucket = {}
    for it in items:
        # url is the identity key for EVERYTHING downstream (memory dedup, outreach join,
        # dashboard link) - an item without one can't be linked or replied to, and two url-less
        # items would silently overwrite each other in the store. Drop them here.
        key = it["url"]
        if not key or key in seen:
            continue
        if len(it["title"]) + len(it["body"]) < MIN_CHARS:
            continue
        seen.add(key)
        # Round-robin by COMMUNITY (subreddit / repo / "Hacker News" / "Bluesky"), not by source,
        # so the MAX_ITEMS cap spreads across INDUSTRIES instead of letting a few big communities
        # dominate.
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


class FatalClassifyError(RuntimeError):
    """A non-transient classify failure (missing/invalid API key, no account access). Aborts the
    WHOLE run so a broken config can't masquerade as a healthy 'found nothing new' run that quietly
    re-deploys the previous (stale) dashboard."""


def _is_auth_error(e):
    """401 (invalid x-api-key) / 403 (no access) from the Anthropic API - a config error, not a
    transient blip. Retrying 200 items against a dead key just burns time and buries the real cause
    under 200 identical log lines. The SDK's error classes carry .status_code."""
    return getattr(e, "status_code", None) in (401, 403)


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
            if _is_auth_error(e):
                raise FatalClassifyError(
                    f"Anthropic API rejected the key ({e}). ANTHROPIC_API_KEY is missing, invalid, "
                    f"or not a Console API key - a Max/Pro subscription does NOT include one. Get a "
                    f"pay-as-you-go key at console.anthropic.com (format sk-ant-api03-...). See README."
                ) from e
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
    c.update(_default_competition())   # every record carries competition fields; high-fit ones get filled
    return c


def classify_all(client, items):
    """Classify all NEW items concurrently. A per-item failure is isolated (dropped + logged), but a
    FATAL error (dead API key) aborts the whole pass at once - grinding 200 items against a bad key
    wastes a minute and buries the cause. Returning [] here (0 survivors) is a real, non-fatal state
    the caller treats as a broken run."""
    results, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = [pool.submit(classify_item, client, it) for it in items]
        try:
            for fut in concurrent.futures.as_completed(futures):
                done += 1
                r = fut.result()   # re-raises FatalClassifyError from the worker thread
                if r:
                    results.append(r)
                print(f"\r  classified {done}/{len(items)}", end="", flush=True)
        except FatalClassifyError:
            for f in futures:
                f.cancel()   # stop queued work; abort fast rather than deploy stale data
            print()
            raise
    print()
    return results


# =============================================================================
# Competition check  -  is this idea already an overcrowded market? (flag only)
# =============================================================================

VALID_COMPETITION = {"open_field", "some_players", "crowded", "saturated", "not_checked"}


def _default_competition():
    return {"competition_level": "not_checked", "competitors": [],
            "comp_rationale": "", "comp_confidence": "unknown", "comp_sanity": ""}


def _all_text(msg):
    """Join ALL text blocks in order. Web-search answers split cited claims (e.g. the
    competitor names we want) across multiple text blocks, so last-block-only would drop them."""
    return "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def _coerce_competition(c):
    if c.get("competition_level") not in VALID_COMPETITION:
        c["competition_level"] = "not_checked"
    comps = c.get("competitors")
    comps = comps if isinstance(comps, list) else []
    c["competitors"] = [str(x).strip()[:100] for x in comps if str(x).strip()][:6]
    c["comp_rationale"] = str(c.get("rationale") or c.get("comp_rationale") or "").strip()[:240]
    c["comp_confidence"] = c.get("confidence") if c.get("confidence") in {"low", "medium", "high"} else "unknown"
    c["comp_sanity"] = str(c.get("sanity_check") or c.get("comp_sanity") or "").strip()[:240]
    # keep only our normalized keys
    return {k: c[k] for k in ("competition_level", "competitors", "comp_rationale", "comp_confidence", "comp_sanity")}


COMPETITION_PROMPT = """A solo founder is weighing whether to build a product that solves this pain:

  "{pain}"  (industry: {industry})

Assess the COMPETITIVE landscape - is this already a served, crowded market, or an open field?

{search_instr}Then SANITY-CHECK yourself: compare what you found against your own prior knowledge. Do they
agree? If web results and your knowledge disagree, say so and lower your confidence.

Return ONLY a strict JSON object (no prose, no fences):
  "competition_level": one of "open_field" (no/weak players), "some_players" (a few, beatable),
                       "crowded" (many established), "saturated" (dominated by strong incumbents).
  "competitors":       up to 6 real named products/tools/services that already address this ("Name - what it does").
  "rationale":         one sentence justifying the level.
  "confidence":        "low" | "medium" | "high".
  "sanity_check":      one sentence - did web search and your own knowledge agree? note any gap."""


def _ask_competition(client, record, use_web):
    """One assessment pass. Web branch gets a bigger token budget (server-injected search
    results eat into it) and a pause_turn loop (long web-search turns pause and must continue)."""
    instr = ("Use web search to find REAL existing products/tools/services that already solve this. "
             if use_web else "Judge from your own knowledge of the market. ")
    prompt = COMPETITION_PROMPT.format(pain=record.get("pain", ""),
                                       industry=record.get("industry", ""), search_instr=instr)
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": COMPETITION_MODEL, "max_tokens": 2500 if use_web else 900,
              "thinking": THINKING_OFF}
    if use_web:
        kwargs["tools"] = [WEB_SEARCH_TOOL]
    msg, texts = None, []
    for _ in range(4):   # continue across pause_turn (long-running web-search turns)
        msg = client.messages.create(messages=messages, **kwargs)
        texts.append(_all_text(msg))   # text BEFORE a pause is part of the answer - keep every turn
        if msg.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": msg.content})
    if msg.stop_reason == "max_tokens":
        print(f"  [competition] response truncated (max_tokens) for {record.get('source_url')}", file=sys.stderr)
    return _coerce_competition(_extract_json("".join(texts)))


def assess_competition(client, record, use_web=True):
    """Research competitors for one high-fit idea. Web search primary; on any error
    (web search disabled/errored/truncated) fall back to a model-knowledge-only pass -
    logged, so a silent always-fallback can't hide."""
    try:
        return _ask_competition(client, record, use_web)
    except Exception as e:
        if use_web:
            print(f"  [competition] web search failed for {record.get('source_url')} ({e}); "
                  f"falling back to model knowledge", file=sys.stderr)
            try:
                return _ask_competition(client, record, use_web=False)
            except Exception as e2:
                e = e2
        print(f"  [competition] gave up on {record.get('source_url')}: {e}", file=sys.stderr)
        return None


def attach_competition(client, results, use_web=True):
    """Gate to high-fit items, research each concurrently, attach the fields in place."""
    targets = [r for r in results if r.get("verdict") in HIGH_FIT_VERDICTS]
    if not targets:
        return
    print(f"Checking competition for {len(targets)} high-fit ideas (web search={'on' if use_web else 'off'})...")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=COMPETITION_WORKERS) as pool:
        fut_map = {pool.submit(assess_competition, client, r, use_web): r for r in targets}
        for fut in concurrent.futures.as_completed(fut_map):
            done += 1
            comp = fut.result()
            if comp:
                fut_map[fut].update(comp)
            print(f"\r  competition {done}/{len(targets)}", end="", flush=True)
    print()


# =============================================================================
# Outreach  -  a ready-to-post discovery reply per high-fit pain
# =============================================================================

OUTREACH_PROMPT = """Someone posted this on {where}:

  Title: {title}
  Text: {body}

The pain identified in it: "{pain}"

Draft ONE short reply (2-4 sentences) the reader could post on that thread to open a genuine
customer-discovery conversation. Hard rules:
- Reference a CONCRETE detail from their post so it reads as written by a human who actually read it.
- Ask exactly ONE open question that gets them talking about their current workflow: how they handle
  it today, how often it bites, or what it costs them (time or money).
- Be honest about intent if context calls for it ("I build tools in this space and I'm trying to
  understand this problem") - NEVER pose as a fellow sufferer, never pitch, never link anything.
- Match the register of the platform ({where}): casual for reddit/Bluesky, technical for GitHub
  issues, direct for Hacker News.

Return ONLY a strict JSON object (no prose, no fences):  {{"comment": "the reply text"}}"""


def _coerce_outreach(raw):
    comment = str((raw or {}).get("comment") or "").strip()
    return comment[:700] if comment else None


def draft_outreach(client, record, item):
    """One discovery-reply draft. Isolated: a failure just leaves the field empty."""
    try:
        prompt = OUTREACH_PROMPT.format(where=item["where"], title=item["title"][:200],
                                        body=item["body"][:BODY_TRUNC], pain=record.get("pain", ""))
        msg = client.messages.create(model=OUTREACH_MODEL, max_tokens=400, thinking=THINKING_OFF,
                                     messages=[{"role": "user", "content": prompt}])
        return _coerce_outreach(_extract_json(_all_text(msg)))
    except Exception as e:
        print(f"  [outreach] gave up on {record.get('source_url')}: {e}", file=sys.stderr)
        return None


def attach_outreach(client, results, items):
    """Draft a discovery reply for each high-fit finding, concurrently. Needs the ORIGINAL item
    text (the classified record only carries the distilled pain), so items are joined by URL."""
    by_url = {it["url"]: it for it in items}
    targets = [(r, by_url[r["source_url"]]) for r in results
               if r.get("verdict") in HIGH_FIT_VERDICTS and r.get("source_url") in by_url]
    if not targets:
        return
    print(f"Drafting discovery replies for {len(targets)} high-fit findings...")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=OUTREACH_WORKERS) as pool:
        fut_map = {pool.submit(draft_outreach, client, r, it): r for r, it in targets}
        for fut in concurrent.futures.as_completed(fut_map):
            done += 1
            comment = fut.result()
            if comment:
                fut_map[fut]["outreach_comment"] = comment
            print(f"\r  outreach {done}/{len(targets)}", end="", flush=True)
    print()


# =============================================================================
# Pain-pattern synthesis  -  each scan ends with a conclusion, not just rows
# =============================================================================

PATTERNS_PROMPT = """You are the discovery co-founder for a solo founder-engineer who builds agentic
investigation systems. Below are the current top pain findings from a market scan across many
industries and sources (reddit, Hacker News, GitHub issues, Bluesky). Each has an index number.

{findings_block}

Synthesize what the market is TELLING US. Find up to {max_patterns} recurring PAIN PATTERNS - a
pattern is the SAME underlying pain shape appearing in more than one finding (across industries or
sources is the strongest signal). Pain first: describe the pain in the sufferers' own framing, not
as a product spec. Only after the pain is nailed, optionally sketch a product direction IF one is
obvious - leave it empty when it is not.

Return ONLY a strict JSON object (no prose, no fences):
{{"patterns": [
  {{
    "name":                 "3-6 word memorable name for the pattern",
    "pain_summary":         "1-2 sentences, the pain in the sufferers' own framing",
    "who_hurts":            "the roles/businesses feeling it",
    "evidence":             [finding index numbers that show this pattern, strongest first],
    "product_direction":    "one optional line - the agentic product this implies, or empty string",
    "discovery_next_step":  "one concrete step to understand this pain better this week"
  }}
]}}

Be ruthless: a pattern needs >= 2 real findings behind it. No padding - fewer good patterns beat
{max_patterns} weak ones."""


def _pattern_inputs(store):
    """Top high-fit findings, compact, for the synthesis prompt. Ordered so index mapping is stable."""
    ranked = sorted((r for r in store["findings"].values() if r.get("verdict") in HIGH_FIT_VERDICTS),
                    key=lambda r: (r.get("fit_score", 0), r.get("times_seen", 1)), reverse=True)
    return ranked[:PATTERNS_INPUT_MAX]


def _spread_line(urls, findings):
    """Cross-source corroboration, computed in Python (not trusted from the model): the same pain
    surfacing in several communities/sources is the strongest sign it is real."""
    recs = [findings[u] for u in urls if u in findings]
    comms = {r.get("where_they_gather", "") for r in recs} - {""}
    srcs = {r.get("source_type", "") for r in recs} - {""}
    return f"{len(recs)} findings / {len(comms)} communities / {len(srcs)} sources"


def _coerce_patterns(raw, inputs, findings):
    """Validate the synthesis output at the boundary; map 1-based evidence indices back to URLs."""
    out = []
    pats = (raw or {}).get("patterns")
    for p in (pats if isinstance(pats, list) else [])[:PATTERNS_MAX]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:80]
        pain = str(p.get("pain_summary") or "").strip()[:400]
        if not (name and pain):
            continue
        idxs = p.get("evidence") if isinstance(p.get("evidence"), list) else []
        urls = []
        for i in idxs:
            if isinstance(i, bool):   # bool IS an int in Python; True would alias finding #1
                continue
            try:
                i = int(i)            # tolerate "3" / 3.0 - models emit both
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(inputs):
                u = inputs[i - 1].get("source_url")
                if u and u not in urls:
                    urls.append(u)
        out.append({
            "name": name,
            "pain_summary": pain,
            "who_hurts": str(p.get("who_hurts") or "").strip()[:200],
            "evidence": urls[:8],
            "spread": _spread_line(urls, findings),
            "product_direction": str(p.get("product_direction") or "").strip()[:200],
            "discovery_next_step": str(p.get("discovery_next_step") or "").strip()[:200],
        })
    return out


def synthesize_patterns(client, store, now):
    """One Sonnet call over the top findings -> store['patterns']. NONFATAL: the table is still
    valuable without the summary, so a failure logs and keeps the previous patterns (stale beats
    absent). This is the run's CONCLUSION - the whole point of scanning from one place."""
    inputs = _pattern_inputs(store)
    if len(inputs) < 2:
        return
    lines = [f"{i}. [{r.get('industry','?')} / {r.get('source_type','?')} / seen x{r.get('times_seen',1)}] "
             f"{r.get('pain','')}" for i, r in enumerate(inputs, 1)]
    prompt = PATTERNS_PROMPT.format(findings_block="\n".join(lines), max_patterns=PATTERNS_MAX)
    print(f"Synthesizing pain patterns from top {len(inputs)} findings...")
    try:
        msg = client.messages.create(model=SYNTH_MODEL, max_tokens=3000, thinking=THINKING_OFF,
                                     messages=[{"role": "user", "content": prompt}])
        items = _coerce_patterns(_extract_json(_all_text(msg)), inputs, store["findings"])
        if items:
            store["patterns"] = {"generated_at": now, "items": items}
            print(f"  {len(items)} patterns: " + "; ".join(p["name"] for p in items))
        else:
            print("  [patterns] synthesis returned no usable patterns; keeping previous", file=sys.stderr)
    except Exception as e:
        print(f"  [patterns] synthesis failed ({e}); keeping previous patterns", file=sys.stderr)


# =============================================================================
# Persistence  -  cross-run memory so the dashboard ACCUMULATES + tracks frequency
# =============================================================================


def load_store(path):
    """Returns {'_meta': {...}, 'findings': {url: record}, 'patterns': {...}?}. Tolerates a
    missing/corrupt file."""
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
# Main
# =============================================================================


def _fetch_all(args):
    raw = []
    for name, fn in [
        ("hackernews", lambda: fetch_hn(HN_QUERIES[:1] if args.sample else HN_QUERIES)),
        ("reddit", lambda: fetch_reddit(INDUSTRIES, sample=args.sample)),
        ("github", lambda: fetch_github(GITHUB_QUERIES[:1] if args.sample else GITHUB_QUERIES)),
        ("bluesky", lambda: fetch_bluesky(BLUESKY_QUERIES[:1] if args.sample else BLUESKY_QUERIES)),
    ]:
        try:
            got = fn()
            print(f"  fetched {len(got)} from {name}")
            raw.extend(got)
        except Exception as e:
            print(f"  [{name}] source failed entirely: {e}", file=sys.stderr)
    return raw


def _classify_new_items(client, new_items, args):
    """Classify + enrich (competition, outreach) the new items. ABORTS the whole run (SystemExit,
    nonzero) rather than deploy stale/empty data when the pipeline is broken: a fatal auth error,
    or a non-trivial batch that classified to zero survivors."""
    print(f"Classifying {len(new_items)} new items with {MODEL}...")
    try:
        results = classify_all(client, new_items)
    except FatalClassifyError as e:
        sys.exit(f"Classification aborted, dashboard NOT updated: {e}")
    # A meaningful batch that classified NOTHING is a broken run (model down / network), not an empty
    # one - exit nonzero so CI goes red instead of re-deploying the previous (stale) dashboard. Tiny
    # batches can hit zero on plain bad luck, so those fall through and the run proceeds as "no new".
    if not results and len(new_items) >= MIN_FATAL_BATCH:
        sys.exit(f"All {len(new_items)} new items failed to classify (0 succeeded); aborting without "
                 f"touching the dashboard. See the log above for the cause.")
    attach_competition(client, results, use_web=not args.no_web_search)
    if not args.no_outreach:
        attach_outreach(client, results, new_items)
    return results


def main():
    ap = argparse.ArgumentParser(description="Personal economy-wide pain-discovery scanner.")
    ap.add_argument("--limit", type=int, default=MAX_ITEMS, help="max NEW items to classify this run")
    ap.add_argument("--sample", action="store_true", help="tiny source subset for a fast smoke test")
    ap.add_argument("--dry-run", action="store_true", help="fetch + prefilter only, no API cost")
    ap.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")
    ap.add_argument("--no-web-search", action="store_true", help="competition check uses model knowledge only (no live web search)")
    ap.add_argument("--no-outreach", action="store_true", help="skip drafting discovery replies")
    ap.add_argument("--no-patterns", action="store_true", help="skip the pain-pattern synthesis")
    ap.add_argument("--out", default="report.html", help="dashboard output path")
    ap.add_argument("--data", default=DATA_PATH, help="cross-run memory JSON path")
    args = ap.parse_args()

    store = load_store(args.data)

    print("Fetching...")
    raw = _fetch_all(args)

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
    client = None
    if new_items:
        client = _make_client()
        results = _classify_new_items(client, new_items, args)
        merge_new(store, results, now)
    else:
        print("No new items to classify.")

    # Synthesize the run's CONCLUSION whenever the evidence changed (or was never summarized).
    if not args.no_patterns and (results or (store["findings"] and "patterns" not in store)):
        client = client or _make_client()
        synthesize_patterns(client, store, now)

    store["_meta"]["last_run"] = now
    save_store(args.data, store)
    run_stats = {"fetched": len(raw), "new_count": len(results),
                 "new_urls": {r["source_url"] for r in results}}
    write_and_open(store, run_stats, args.out, not args.no_open)
    print(f"Memory: {Path(args.data).resolve()} ({len(store['findings'])} findings)")


def _make_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set - needs a Console pay-as-you-go key "
                 "(sk-ant-api03-... from console.anthropic.com). export it and retry (or use --dry-run).")
    import anthropic
    return anthropic.Anthropic()   # one client, shared across all judgment threads


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
