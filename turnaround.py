#!/usr/bin/env python3
"""
Turnaround Hunter — automated 52-week-low recovery scan.

Sweeps the small/mid-cap universe on screener.in for *beaten-down turnarounds*:
companies trading near their 52-week LOW where the business is visibly improving
— the last four quarters' EBITDA (Operating Profit) is positive and the latest
quarter is growing — with an optional overlay for sectors the user believes are
turning around on domestic + global factors.

Pipeline:
  1. screen_universe()  — query screener.in's screen for the small/mid-cap band
     with positive, YoY-improving operating profit (shrinks the universe server-side).
  2. analyze()          — per company: market cap, % above 52-week low, the last
     four quarters of Operating Profit / OPM / Sales, and (best-effort) the sector.
  3. gate + score()     — keep only names that clear the turnaround filter, then
     rank them 0-100 and write a Markdown report (and optional email).

This is a QUANTITATIVE screen. EBITDA is approximated by screener's "Operating
Profit" line; "business improving" and "sector turnaround" are heuristics. It does
NOT read concalls or annual reports — verify before acting.

Analytical research, NOT investment advice.
"""

import os
import re
import sys
import json
import time
import datetime
from urllib.parse import quote_plus

import requests

# Reuse the proven screener.in parsing helpers from the existing scanner so the
# two scans stay consistent and we don't duplicate fragile scraping logic.
from scan import (
    HEADERS,
    fetch,
    parse_top_ratios,
    row_values,
    _section_table,
    fmt,
    send_email,
)

# --------------------------------------------------------------------------- #
# Config (env-overridable so the workflow / user can tune without code edits)
# --------------------------------------------------------------------------- #
MCAP_MIN = float(os.environ.get("MCAP_MIN", 500))        # Rs cr — drop micro-caps
MCAP_MAX = float(os.environ.get("MCAP_MAX", 75000))      # Rs cr — drop large-caps
NEAR_LOW_PCT = float(os.environ.get("NEAR_LOW_PCT", 15))  # % above 52w low to qualify
MAX_PAGES = int(os.environ.get("MAX_PAGES", 10))         # screen pages (25 names/page)
PAGE_DELAY = float(os.environ.get("PAGE_DELAY", 0.7))    # polite delay between requests

SCREEN_URL = "https://www.screener.in/screen/raw/"

# Default screen query. Overridable via screen_query.txt. Pushes the market-cap
# band and the "EBITDA positive and improving YoY" cut to screener's server so we
# fetch far fewer company pages.
DEFAULT_SCREEN_QUERY = (
    f"Market Capitalization < {MCAP_MAX:g} AND "
    f"Market Capitalization > {MCAP_MIN:g} AND "
    "Operating profit latest quarter > 0 AND "
    "Operating profit latest quarter > Operating profit preceding year quarter"
)


# --------------------------------------------------------------------------- #
# Universe — screener.in screen
# --------------------------------------------------------------------------- #
def load_screen_query():
    path = os.path.join(os.path.dirname(__file__), "screen_query.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip() and not ln.lstrip().startswith("#")]
        if lines:
            # Allow the query to be split across lines for readability.
            return " ".join(lines)
    return DEFAULT_SCREEN_QUERY


def _codes_from_results(html):
    """Extract screener company codes from a screen-results page, in order."""
    codes, seen = [], set()
    # Anchors look like /company/CODE/ or /company/CODE/consolidated/ — grab the
    # code up to the next slash/quote/query regardless of any trailing path.
    for m in re.finditer(r'/company/([^/"?]+)', html):
        code = m.group(1)
        if code and code.upper() not in seen and code != "compare":
            seen.add(code.upper())
            codes.append(code)
    return codes


def screen_universe(query, max_pages):
    """Return a de-duplicated list of screener codes matching `query`."""
    all_codes, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{SCREEN_URL}?query={quote_plus(query)}&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"  screen page {page} failed: {e}")
            break
        if r.status_code != 200:
            print(f"  screen page {page} -> HTTP {r.status_code}; stopping.")
            break
        page_codes = _codes_from_results(r.text)
        new = [c for c in page_codes if c.upper() not in seen]
        if not new:
            break                       # no more results
        for c in new:
            seen.add(c.upper())
            all_codes.append(c)
        print(f"  screen page {page}: +{len(new)} (total {len(all_codes)})")
        time.sleep(PAGE_DELAY)
    return all_codes


def load_watchlist_fallback():
    """Fall back to watchlist.txt so a run never comes back empty."""
    path = os.path.join(os.path.dirname(__file__), "watchlist.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [ln.strip().upper() for ln in f
                    if ln.strip() and not ln.startswith("#")]
    return []


# --------------------------------------------------------------------------- #
# Per-company parsing
# --------------------------------------------------------------------------- #
def quarterly(soup):
    """Parse the Quarterly Results section into EBITDA / OPM / Sales trends.

    screener lists quarters oldest->newest left to right, so the LAST value is
    the most recent quarter and value [-5] is the year-ago quarter.
    """
    tbl = _section_table(soup, "quarters")
    op = [v for v in row_values(tbl, "Operating Profit") if v is not None]
    opm = [v for v in row_values(tbl, "OPM %") if v is not None]
    sales = [v for v in row_values(tbl, "Sales") if v is not None]

    out = {
        "op": op, "opm": opm, "sales": sales,
        "op_last4": None, "op_all_pos_4q": False,
        "op_latest": None, "op_qoq": None, "op_yoy": None,
        "op_recovery": None, "opm_latest": None, "opm_yoy_delta": None,
        "improving": False,
    }
    if len(op) < 4:
        return out

    last4 = op[-4:]
    latest = op[-1]
    prev = op[-2]
    year_ago = op[-5] if len(op) >= 5 else None
    trough4 = min(last4)

    out["op_last4"] = last4
    out["op_all_pos_4q"] = all(v > 0 for v in last4)
    out["op_latest"] = latest
    out["op_qoq"] = round((latest / prev - 1) * 100, 1) if prev else None
    if year_ago and year_ago > 0:
        out["op_yoy"] = round((latest / year_ago - 1) * 100, 1)
    if trough4 > 0:
        out["op_recovery"] = round(latest / trough4, 2)
    if opm:
        out["opm_latest"] = opm[-1]
        if len(opm) >= 5:
            out["opm_yoy_delta"] = round(opm[-1] - opm[-5], 1)

    # "Improving" = YoY EBITDA growth, OR latest is the best of the last four and
    # above the prior quarter (a fresh sequential up-leg off the bottom).
    yoy_up = out["op_yoy"] is not None and out["op_yoy"] > 0
    seq_up = latest == max(last4) and latest > prev
    out["improving"] = bool(yoy_up or seq_up)
    return out


def near_low(top):
    """Return (pct_above_low, low, high) from the top-ratios 'High / Low' block."""
    hl = top.get("High / Low") or top.get("High/Low")
    price = (top.get("Current Price") or [None])[0]
    if not hl or len(hl) < 2 or price is None:
        return None, None, None
    high, low = hl[0], hl[1]
    if not low or low <= 0:
        return None, low, high
    return round((price / low - 1) * 100, 1), low, high


def parse_sector(soup):
    """Best-effort sector/industry string from the peers section. None on miss."""
    sec = soup.find(id="peers")
    if not sec:
        return None
    # Screener links the sector/industry to /company/compare/<id>/<name>/.
    labels = []
    for a in sec.find_all("a", href=True):
        if "/company/compare/" in a["href"]:
            txt = a.get_text(strip=True)
            if txt and txt.lower() not in (l.lower() for l in labels):
                labels.append(txt)
    return " / ".join(labels[:2]) if labels else None


# --------------------------------------------------------------------------- #
# Sector turnaround overlay
# --------------------------------------------------------------------------- #
def load_sectors():
    path = os.path.join(os.path.dirname(__file__), "sectors.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("turnaround_sectors", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def sector_note(sector, turnaround_sectors):
    """Return the rationale string if `sector` matches a turnaround sector."""
    if not sector:
        return None
    s = sector.lower()
    for key, note in turnaround_sectors.items():
        k = key.lower()
        if k in s or s in k:
            return note
    return None


# --------------------------------------------------------------------------- #
# Analyze + score
# --------------------------------------------------------------------------- #
def analyze(ticker, turnaround_sectors):
    url, soup = fetch(ticker)
    if soup is None:
        return {"ticker": ticker, "url": url, "ok": False}

    top = parse_top_ratios(soup)

    def top_val(label):
        v = top.get(label)
        return v[0] if v else None

    pct_above_low, low, high = near_low(top)
    q = quarterly(soup)
    sector = parse_sector(soup)
    note = sector_note(sector, turnaround_sectors)

    name_tag = soup.find("h1")
    return {
        "ticker": ticker,
        "url": url,
        "ok": True,
        "name": name_tag.get_text(strip=True) if name_tag else ticker,
        "mcap": top_val("Market Cap"),
        "price": top_val("Current Price"),
        "pe": top_val("Stock P/E"),
        "roce": top_val("ROCE"),
        "pct_above_low": pct_above_low,
        "low52": low,
        "high52": high,
        "sector": sector,
        "sector_note": note,
        **q,
    }


def qualifies(m):
    """Gating filter — must clear all to make the report."""
    mcap = m.get("mcap")
    pal = m.get("pct_above_low")
    if mcap is None or not (MCAP_MIN <= mcap <= MCAP_MAX):
        return False
    if pal is None or pal > NEAR_LOW_PCT:
        return False
    if not m.get("op_all_pos_4q"):
        return False
    if not m.get("improving"):
        return False
    return True


def _clamp(x):
    return max(0, min(100, x))


def score(m):
    """0-100 turnaround score: how cheap (vs 52w low) + how real the recovery is."""
    pal = m.get("pct_above_low")
    yoy = m.get("op_yoy")
    rec = m.get("op_recovery")
    opm_d = m.get("opm_yoy_delta")

    # Proximity to 52-week low (closer to the low = more interesting setup).
    if pal is None:
        px = 40
    elif pal <= 3:
        px = 100
    elif pal <= 7:
        px = 85
    elif pal <= 10:
        px = 70
    elif pal <= 15:
        px = 55
    else:
        px = 35

    # EBITDA YoY growth in the latest quarter.
    if yoy is None:
        gy = 45
    elif yoy >= 50:
        gy = 92
    elif yoy >= 25:
        gy = 78
    elif yoy >= 10:
        gy = 62
    elif yoy > 0:
        gy = 50
    else:
        gy = 30

    # Trough-to-latest recovery over the last four quarters.
    if rec is None:
        rc = 45
    elif rec >= 2.0:
        rc = 88
    elif rec >= 1.5:
        rc = 74
    elif rec >= 1.2:
        rc = 62
    elif rec >= 1.0:
        rc = 50
    else:
        rc = 35

    # OPM expansion YoY (margin-led, higher-quality turn).
    if opm_d is None:
        om = 50
    elif opm_d >= 5:
        om = 85
    elif opm_d >= 2:
        om = 70
    elif opm_d >= 0:
        om = 55
    else:
        om = 38

    total = 0.30 * px + 0.28 * gy + 0.24 * rc + 0.18 * om
    if m.get("sector_note"):
        total += 6           # sector-turnaround overlay bonus
    return round(_clamp(total), 1)


def verdict(total):
    if total >= 70:
        return "Strong turnaround setup"
    if total >= 55:
        return "Watch"
    if total >= 45:
        return "Early / unproven"
    return "Pass"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _trend(vals):
    if not vals:
        return "n/a"
    return " → ".join(fmt(v) for v in vals)


def build_report(rows, today, scanned, query):
    lines = []
    lines.append("# Turnaround Hunter — 52-Week-Low Recovery Scan\n")
    lines.append(
        f"**Date:** {today} · **Universe:** small/mid-caps Rs {MCAP_MIN:g}–{MCAP_MAX:g} cr "
        f"· **Near-low cut:** ≤{NEAR_LOW_PCT:g}% above 52w low · **Source:** screener.in\n")
    lines.append(
        "> Analytical research, **NOT investment advice**. EBITDA ≈ screener's "
        "\"Operating Profit\"; \"improving\" and \"sector turnaround\" are heuristics "
        "from numbers + an editable overlay. Verify concalls/filings before acting.\n")
    lines.append(
        f"Scanned **{scanned}** names from the screen; **{len(rows)}** cleared the "
        "turnaround filter (near 52w low · 4 quarters of positive EBITDA · latest "
        "quarter improving).\n")

    if not rows:
        lines.append("_No names cleared the filter this run._\n")
        lines.append(f"\n*Screen query:* `{query}`")
        return "\n".join(lines)

    lines.append("## Ranked setups\n")
    lines.append("| # | Company | Ticker | Mcap (cr) | % above 52w low | "
                 "EBITDA YoY | Recovery×trough | Score | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['name']} | {r['ticker']} | {fmt(r['mcap'])} | "
            f"{fmt(r['pct_above_low'], '%')} | {fmt(r['op_yoy'], '%')} | "
            f"{fmt(r['op_recovery'], '×')} | **{r['total']}** | {r['verdict']} |")

    lines.append("\n## Detail — EBITDA / OPM trend & sector\n")
    for r in rows:
        lines.append(f"### {r['name']} ({r['ticker']}) — score {r['total']}")
        lines.append(
            f"- Price {fmt(r['price'])} · 52w low {fmt(r['low52'])} / high "
            f"{fmt(r['high52'])} · **{fmt(r['pct_above_low'], '%')} above low**")
        lines.append(f"- Operating profit (last quarters): {_trend(r['op'][-6:])}")
        lines.append(f"- OPM % (last quarters): {_trend(r['opm'][-6:])}"
                     f" · YoY Δ {fmt(r['opm_yoy_delta'], 'pp')}")
        lines.append(f"- EBITDA YoY {fmt(r['op_yoy'], '%')} · QoQ "
                     f"{fmt(r['op_qoq'], '%')} · recovery from 4q trough "
                     f"{fmt(r['op_recovery'], '×')}")
        if r.get("sector"):
            line = f"- Sector: {r['sector']}"
            if r.get("sector_note"):
                line += f" — **turnaround overlay:** {r['sector_note']}"
            lines.append(line)
        lines.append(f"- [screener.in]({r['url']})\n")

    lines.append("\n## Sources\n")
    for r in rows:
        lines.append(f"- [{r['ticker']}]({r['url']})")
    lines.append(f"\n*Screen query:* `{query}`")
    lines.append("\n*Score = 30% proximity-to-low + 28% EBITDA YoY + 24% "
                 "trough-recovery + 18% OPM expansion (+6 sector overlay). "
                 "Verdict: ≥70 Strong · 55–69 Watch · 45–54 Early · <45 Pass. "
                 "Not investment advice.*")
    return "\n".join(lines)


def build_email_html(rows, today):
    head = (
        '<div style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:820px">'
        f'<h2>Turnaround Hunter — 52-week-low scan ({today})</h2>'
        '<p style="background:#fff8e1;border-left:4px solid #f0ad4e;padding:8px 12px;'
        'font-size:13px">Small/mid-caps near their 52-week low with improving EBITDA. '
        '<b>Not investment advice.</b></p>')
    if not rows:
        return head + "<p>No names cleared the filter this run.</p></div>"
    head += (
        '<table cellpadding="6" cellspacing="0" border="1" '
        'style="border-collapse:collapse;font-size:13px">'
        '<tr style="background:#1f3a5f;color:#fff"><th>#</th><th>Company</th>'
        '<th>Ticker</th><th>Mcap</th><th>% above low</th><th>EBITDA YoY</th>'
        '<th>Score</th><th>Verdict</th></tr>')
    body = ""
    for i, r in enumerate(rows, 1):
        body += (f"<tr><td>{i}</td><td>{r['name']}</td><td>{r['ticker']}</td>"
                 f"<td>{fmt(r['mcap'])}</td><td>{fmt(r['pct_above_low'], '%')}</td>"
                 f"<td>{fmt(r['op_yoy'], '%')}</td><td><b>{r['total']}</b></td>"
                 f"<td>{r['verdict']}</td></tr>")
    return head + body + "</table></div>"


# --------------------------------------------------------------------------- #
def main():
    today = datetime.date.today().isoformat()
    query = load_screen_query()
    turnaround_sectors = load_sectors()

    print(f"Screen query: {query}")
    universe = screen_universe(query, MAX_PAGES)
    if not universe:
        universe = load_watchlist_fallback()
        print(f"Screen returned nothing — falling back to watchlist "
              f"({len(universe)} names).")
    print(f"Universe: {len(universe)} names. Analysing...")

    rows = []
    for t in universe:
        m = analyze(t, turnaround_sectors)
        if m.get("ok") and qualifies(m):
            m["total"] = score(m)
            m["verdict"] = verdict(m["total"])
            rows.append(m)
            print(f"  ✓ {t}: {m['pct_above_low']}% above low, "
                  f"EBITDA YoY {m['op_yoy']}%, score {m['total']}")
        time.sleep(PAGE_DELAY)

    rows.sort(key=lambda r: r["total"], reverse=True)
    report = build_report(rows, today, len(universe), query)

    reports_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    dated = os.path.join(reports_dir, f"turnaround-{today}.md")
    latest = os.path.join(reports_dir, "turnaround-latest.md")
    for path in (dated, latest):
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    print(f"Wrote {dated} ({len(rows)} qualifying names)")

    subject = (f"Turnaround Scan ({today}): {len(rows)} small/mid-caps near 52w low "
               "with improving EBITDA")
    if rows:
        subject = (f"Turnaround Scan ({today}): {rows[0]['name']} tops "
                   f"@ {rows[0]['total']} ({len(rows)} names)")
    send_email(subject, build_email_html(rows, today))


if __name__ == "__main__":
    main()
