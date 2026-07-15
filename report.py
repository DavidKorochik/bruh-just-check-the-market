"""Dashboard + digest rendering. Pure presentation - no network, no API.

The dashboard is the product's thesis made visible: pain is distributed across many
places, so this is the ONE structured, summarized place. Top to bottom it answers,
in order: "what is the market telling me?" (pain-pattern cards), "how much evidence?"
(summary cards), "show me the receipts" (the filterable findings table, each with a
ready-to-post discovery reply).
"""

from __future__ import annotations

import datetime as dt
import html
import webbrowser
from pathlib import Path

VERDICT_COLOR = {"worth_a_call": "#1a7f37", "watch": "#b7791f", "skip": "#6b7280"}
# green = open field (good), red = saturated (bad) - the market's verdict alongside the pain's
COMPETITION_COLOR = {"open_field": "#1a7f37", "some_players": "#0969da", "crowded": "#b7791f",
                     "saturated": "#cf222e", "not_checked": "#6b7280"}
SOURCE_LABEL = {"reddit": "reddit", "hackernews": "hackernews", "github": "github", "bluesky": "bluesky"}


def _safe_href(url):
    """Allow ONLY http(s). Blocks javascript:/data: scheme injection from scraped URLs."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def _e(s):
    return html.escape(str(s or ""))


# =============================================================================
# Pain-pattern cards - the synthesized "what is the market telling me" layer
# =============================================================================


def _render_patterns(store):
    pat = store.get("patterns") or {}
    items = pat.get("items") or []
    if not items:
        return ""
    findings = store.get("findings", {})
    cards = []
    for p in items:
        ev_links = []
        for j, u in enumerate(p.get("evidence", []), 1):
            href = _safe_href(u)
            if not href:
                continue
            f = findings.get(u, {})
            tip = _e(f.get("pain", u))
            ev_links.append(f'<a href="{_e(href)}" target="_blank" rel="noopener" title="{tip}">[{j}]</a>')
        spread = p.get("spread", "")
        direction = p.get("product_direction", "")
        direction_html = (f'<div class="pdir"><span class="plabel">possible product</span> {_e(direction)}</div>'
                          if direction else "")
        next_step = p.get("discovery_next_step", "")
        next_html = (f'<div class="pnext"><span class="plabel">next</span> {_e(next_step)}</div>'
                     if next_step else "")
        cards.append(f"""
    <div class="pattern">
      <div class="pname">{_e(p.get('name', ''))}</div>
      <div class="ppain">&ldquo;{_e(p.get('pain_summary', ''))}&rdquo;</div>
      <div class="pwho">{_e(p.get('who_hurts', ''))}{' &middot; ' + _e(spread) if spread else ''}
        &middot; evidence: {' '.join(ev_links) if ev_links else '-'}</div>
      {direction_html}{next_html}
    </div>""")
    gen = _e(pat.get("generated_at", "")[:16].replace("T", " "))
    return f"""
  <h2 class="ptitle">Pain patterns <span class="psub">what the market is telling you &middot; synthesized {gen}</span></h2>
  <div class="patterns">{''.join(cards)}</div>"""


# =============================================================================
# Findings table
# =============================================================================


def _comment_row(r, idx):
    """Hidden full-width row under a finding: the ready-to-post discovery reply + copy button."""
    comment = (r.get("outreach_comment") or "").strip()
    if not comment:
        return "", ""
    btn = (f'<button class="cbtn" onclick="toggleComment({idx})" '
           f'title="a discovery reply you can post on the source thread">&#128172;</button>')
    row = (f'<tr class="crow" id="crow{idx}" style="display:none"><td colspan="11">'
           f'<div class="cbox"><span class="clabel">draft discovery reply</span>'
           f'<button class="copy" onclick="copyComment({idx})">copy</button>'
           f'<div class="ctext" id="ctext{idx}">{_e(comment)}</div></div></td></tr>')
    return btn, row


def _finding_rows(findings, new_urls):
    rows = []
    for idx, r in enumerate(findings):
        v = r.get("verdict", "skip")
        color = VERDICT_COLOR.get(v, "#6b7280")
        href = _safe_href(r.get("source_url", ""))
        src = _e(r.get("source_type", ""))
        src_cell = (f'<a href="{_e(href)}" target="_blank" rel="noopener">{src} &#8599;</a>'
                    if href else src)
        is_new = r.get("source_url") in new_urls
        newbadge = '<span class="new">NEW</span>' if is_new else ""
        seen = r.get("times_seen", 1)
        comp = r.get("competition_level", "not_checked")
        competitors = r.get("competitors", []) or []
        conf = r.get("comp_confidence", "unknown")
        if comp == "not_checked":
            comp_cell = '<span class="dim">-</span>'
        else:
            tip = r.get("comp_rationale", "")
            if competitors:
                tip = (tip + " | " if tip else "") + "; ".join(competitors)
            if r.get("comp_sanity"):
                tip += f"  (sanity: {r['comp_sanity']})"
            conf_sup = f'<span class="conf" title="confidence">{_e(conf[0])}</span>' if conf != "unknown" else ""
            comp_cell = (f'<span class="badge" style="background:{COMPETITION_COLOR.get(comp, "#6b7280")}" '
                         f'title="{_e(tip[:400])}">{_e(comp.replace("_", " "))}</span>{conf_sup}')
        search_blob = _e(f"{r.get('pain','')} {r.get('quote','')} {r.get('where_they_gather','')} "
                         f"{r.get('industry','')} {' '.join(competitors)}".lower())
        cbtn, crow = _comment_row(r, idx)
        rows.append(f"""
      <tr data-verdict="{_e(v)}" data-industry="{_e(r.get('industry','other'))}" data-wtp="{_e(r.get('wtp_tier',''))}"
          data-source="{src}" data-competition="{_e(comp)}" data-new="{'1' if is_new else '0'}" data-search="{search_blob}">
        <td class="num">{r.get('fit_score',0)}{newbadge}</td>
        <td><span class="badge" style="background:{color}">{_e(v)}</span></td>
        <td>{comp_cell}</td>
        <td>{_e(r.get('industry','other'))}</td>
        <td>{_e(r.get('wtp_tier',''))}</td>
        <td>{_e(r.get('pain',''))}</td>
        <td class="quote">{_e(r.get('quote',''))}</td>
        <td>{_e(r.get('where_they_gather',''))}</td>
        <td class="num" title="times this pain resurfaced across runs">{seen}</td>
        <td>{src_cell}</td>
        <td>{cbtn}</td>
      </tr>{crow}""")
    return rows


def render_html(store, run_stats):
    findings = sorted(store["findings"].values(), key=lambda r: r.get("fit_score", 0), reverse=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {"worth_a_call": 0, "watch": 0, "skip": 0}
    industries = {}
    sources = {}
    # "open lane" = a worth-a-call idea that is NOT already a crowded/saturated market
    open_lane = 0
    for r in findings:
        counts[r.get("verdict", "skip")] = counts.get(r.get("verdict", "skip"), 0) + 1
        industries[r.get("industry", "other")] = industries.get(r.get("industry", "other"), 0) + 1
        sources[r.get("source_type", "")] = sources.get(r.get("source_type", ""), 0) + 1
        if r.get("verdict") == "worth_a_call" and r.get("competition_level") in ("open_field", "some_players"):
            open_lane += 1
    new_urls = run_stats.get("new_urls", set())

    ind_options = "".join(f'<option value="{_e(k)}">{_e(k)} ({v})</option>'
                          for k, v in sorted(industries.items(), key=lambda kv: -kv[1]))
    # source filter built from the DATA, so new sources appear without touching this file
    src_options = "".join(f'<option value="{_e(k)}">{_e(SOURCE_LABEL.get(k, k))} ({v})</option>'
                          for k, v in sorted(sources.items(), key=lambda kv: -kv[1]) if k)

    rows = _finding_rows(findings, new_urls)

    def card(n, label, color=None):
        style = f' style="color:{color}"' if color else ""
        return f'<div class="card"><div class="n"{style}>{n}</div><div class="l">{label}</div></div>'

    cards = "".join([
        card(len(findings), "total findings"),
        card(counts["worth_a_call"], "worth a call", VERDICT_COLOR["worth_a_call"]),
        card(open_lane, "worth a call + open lane", COMPETITION_COLOR["open_field"]),
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
  .ptitle {{ font-size: 16px; margin: 4px 0 10px; }}
  .psub {{ color: var(--dim); font-size: 12px; font-weight: 400; }}
  .patterns {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 12px; margin-bottom: 22px; }}
  .pattern {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }}
  .pname {{ font-weight: 700; margin-bottom: 4px; }}
  .ppain {{ font-style: italic; color: var(--dim); margin-bottom: 6px; }}
  .pwho {{ font-size: 13px; margin-bottom: 6px; }}
  .pdir, .pnext {{ font-size: 13px; margin-top: 4px; }}
  .plabel {{ font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--accent);
    border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; margin-right: 6px; }}
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
  .conf {{ color: var(--dim); font-size: 10px; margin-left: 4px; text-transform: uppercase; cursor: help; }}
  .dim {{ color: var(--dim); }}
  .badge[title] {{ cursor: help; }}
  a {{ color: var(--accent); text-decoration: none; }}
  tr:hover td {{ background: var(--panel); }}
  .cbtn {{ background: none; border: 1px solid var(--line); border-radius: 6px; cursor: pointer;
    font-size: 13px; padding: 2px 7px; }}
  .crow td {{ background: var(--panel); }}
  .cbox {{ position: relative; max-width: 720px; }}
  .clabel {{ font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--accent); }}
  .ctext {{ white-space: pre-wrap; font-size: 14px; margin-top: 4px; }}
  .copy {{ position: absolute; right: 0; top: 0; background: var(--bg); color: var(--fg);
    border: 1px solid var(--line); border-radius: 6px; font-size: 11px; padding: 2px 8px; cursor: pointer; }}
</style></head>
<body>
  <h1>Ask My Market</h1>
  <div class="sub">Run {ts} &middot; {run_stats.get('fetched',0)} fetched &middot;
    {run_stats.get('new_count',0)} newly classified &middot; {len(findings)} total in memory</div>
{_render_patterns(store)}
  <div class="cards">{cards}</div>

  <div class="controls">
    <select id="fVerdict"><option value="">all verdicts</option>
      <option value="worth_a_call">worth a call</option><option value="watch">watch</option><option value="skip">skip</option></select>
    <select id="fCompetition"><option value="">all competition</option>
      <option value="open_field">open field</option><option value="some_players">some players</option>
      <option value="crowded">crowded</option><option value="saturated">saturated</option>
      <option value="not_checked">not checked</option></select>
    <select id="fIndustry"><option value="">all industries</option>{ind_options}</select>
    <select id="fWtp"><option value="">all wtp tiers</option>
      <option value="paying_a_human">paying a human</option><option value="paying_for_bad_tool">paying for bad tool</option>
      <option value="just_complaining">just complaining</option></select>
    <select id="fSource"><option value="">all sources</option>{src_options}</select>
    <input type="search" id="fSearch" placeholder="search pain / quote / where / industry...">
    <label class="chk"><input type="checkbox" id="fNew"> new only</label>
    <span class="count" id="count"></span>
  </div>

  <table>
    <thead><tr>
      <th>fit</th><th>verdict</th><th title="market saturation from live web search">competition</th>
      <th>industry</th><th>wtp tier</th><th>pain</th>
      <th>sharpest quote</th><th>where they gather</th><th>seen</th><th>source</th>
      <th title="draft discovery reply">&#128172;</th>
    </tr></thead>
    <tbody id="tb">{''.join(rows) if rows else '<tr><td colspan="11">No findings yet. Run a scan.</td></tr>'}</tbody>
  </table>
<script>
  var rows = Array.prototype.slice.call(document.querySelectorAll('#tb tr[data-verdict]'));
  ['fVerdict','fCompetition','fIndustry','fWtp','fSource','fSearch','fNew'].forEach(function(id){{
    var el=document.getElementById(id); if(el) el.addEventListener('input', apply);
  }});
  function apply() {{
    var v=val('fVerdict'), c=val('fCompetition'), i=val('fIndustry'), w=val('fWtp'), s=val('fSource'),
        q=val('fSearch').toLowerCase(), n=document.getElementById('fNew').checked;
    var shown=0;
    rows.forEach(function(r){{
      var ok = (!v||r.dataset.verdict===v) && (!c||r.dataset.competition===c) && (!i||r.dataset.industry===i)
            && (!w||r.dataset.wtp===w) && (!s||r.dataset.source===s) && (!n||r.dataset.new==='1')
            && (!q||r.dataset.search.indexOf(q)>-1);
      r.style.display = ok ? '' : 'none';
      if(!ok) hideComment(r);
      if(ok) shown++;
    }});
    document.getElementById('count').textContent = shown + ' / ' + rows.length + ' shown';
  }}
  function val(id){{ return document.getElementById(id).value; }}
  function toggleComment(i){{
    var r=document.getElementById('crow'+i);
    if(r) r.style.display = (r.style.display==='none') ? '' : 'none';
  }}
  function hideComment(findingRow){{
    var next=findingRow.nextElementSibling;
    if(next && next.classList.contains('crow')) next.style.display='none';
  }}
  function copyComment(i){{
    var t=document.getElementById('ctext'+i);
    if(t && navigator.clipboard) navigator.clipboard.writeText(t.textContent);
  }}
  apply();
</script>
</body></html>"""


# =============================================================================
# Digest - the per-scan conclusion, as markdown (CI job summary + Pages /digest.md)
# =============================================================================


def _md(s):
    """Neutralize untrusted text for markdown sinks (the GitHub Actions job summary renders full
    markdown incl. links/images). Kills the syntax that could inject: brackets, backticks, raw
    HTML, and newlines (a newline would let scraped text fabricate its own headings/items)."""
    s = str(s or "")
    for ch, rep in (("[", "("), ("]", ")"), ("`", "'"), ("<", "("), (">", ")")):
        s = s.replace(ch, rep)
    return " ".join(s.split())


def build_digest(store, run_stats):
    """A short markdown answer to 'what did this scan conclude?'. Lands in the GitHub Actions
    job summary (visible on the run page without opening the dashboard) and on Pages."""
    findings = sorted(store["findings"].values(), key=lambda r: r.get("fit_score", 0), reverse=True)
    new_urls = run_stats.get("new_urls", set())
    lines = [f"## Scan digest",
             f"",
             f"{run_stats.get('fetched', 0)} fetched, {run_stats.get('new_count', 0)} newly classified, "
             f"{len(findings)} findings in memory.",
             f""]
    items = (store.get("patterns") or {}).get("items") or []
    if items:
        lines.append("### Pain patterns")
        lines.append("")
        for p in items:
            direction = f" *Possible product: {_md(p['product_direction'])}*" if p.get("product_direction") else ""
            lines.append(f"- **{_md(p.get('name', ''))}** - {_md(p.get('pain_summary', ''))} "
                         f"({_md(p.get('spread', '')) or 'evidence: ' + str(len(p.get('evidence', [])))}){direction}")
        lines.append("")
    top_new = [r for r in findings if r.get("source_url") in new_urls
               and r.get("verdict") == "worth_a_call"][:5]
    if top_new:
        lines.append("### New worth-a-call findings")
        lines.append("")
        for r in top_new:
            lines.append(f"- [{r.get('fit_score', 0)}] {_md(r.get('pain', ''))} "
                         f"({_md(r.get('industry', ''))}, {_md(r.get('where_they_gather', ''))})")
        lines.append("")
    return "\n".join(lines)


def write_and_open(store, run_stats, out_path, open_browser):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(store, run_stats), encoding="utf-8")
    digest = build_digest(store, run_stats)
    (out.parent / "digest.md").write_text(digest, encoding="utf-8")
    print(f"Dashboard: {out.resolve()}")
    print("\n" + digest)
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
