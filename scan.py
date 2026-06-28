#!/usr/bin/env python3
"""
Multibagger Hunter (India — NSE/BSE) — automated daily scan.

Acts as a quantitative Chartered-Accountant / equity-analyst screen. The goal is
NOT to predict prices — it is to surface businesses with the structural traits of
past Indian multibaggers and to ruthlessly disqualify likely accounting frauds and
value traps that dominate the small-cap space.

For each name on the watchlist it:
  1. Scrapes screener.in (10-12y financials, shareholding, ratios).
  2. Runs the HARD RED-FLAG forensic screen FIRST. 2+ hard flags => AVOID.
  3. Scores the six pillars (Size, Quality, Growth, Longevity, Management, Price)
     using references/scoring-rubric.md, weighted and summed.
  4. Emits the standard report: verdict box, red-flag table, pillar scorecard,
     multibagger-math table, kill-switches, sources — plus a ranked comparison.

Runs on GitHub Actions on a daily schedule, so it fires on GitHub's servers even
when your own machine is off. Optionally emails the summary via Gmail SMTP when
SMTP_USER / SMTP_PASS / EMAIL_TO repo secrets are set.

This reads NUMBERS ONLY — it does not read annual reports or concalls. So the
Longevity (moat), the candor part of Management, and red flags 5-8/10 are heuristic
proxies flagged for human review. Analytical research, NOT investment advice.
"""

import os
import re
import sys
import datetime
import smtplib
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Default watchlist (screener.in codes). Override by editing watchlist.txt
# with one ticker per line.
DEFAULT_WATCHLIST = [
    "TIPSMUSIC", "CAPLIPOINT", "GRAVITA", "NEWGEN", "ANANDRATHI",
    "GARFIBRES", "CONTROLPR", "PIXTRANS", "RACLGEAR", "SUPRAJIT",
]

WEIGHTS = {"S": 0.15, "Q": 0.25, "G": 0.20, "L": 0.15, "M": 0.15, "P": 0.10}
PILLAR_NAMES = {
    "S": "Size & obscurity",
    "Q": "Quality of business",
    "G": "Growth",
    "L": "Longevity / moat",
    "M": "Management & capital allocation",
    "P": "Price",
}


# --------------------------------------------------------------------------- #
# Fetch + parse helpers
# --------------------------------------------------------------------------- #
def fetch(ticker):
    """Return (url, soup) trying consolidated then standalone view."""
    last_url = ""
    for path in (f"/company/{ticker}/consolidated/", f"/company/{ticker}/"):
        last_url = "https://www.screener.in" + path
        try:
            r = requests.get(last_url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code == 200 and "Compounded" in r.text:
            return last_url, BeautifulSoup(r.text, "html.parser")
    return last_url, None


def _to_float(text):
    if text is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text).replace(",", ""))
    return float(m.group()) if m else None


def parse_top_ratios(soup):
    """Parse the #top-ratios block into {label: [numbers]}."""
    out = {}
    ul = soup.find(id="top-ratios")
    if not ul:
        return out
    for li in ul.find_all("li"):
        name = li.find(class_="name")
        if not name:
            continue
        nums = [_to_float(n.get_text()) for n in li.find_all(class_="number")]
        out[name.get_text(strip=True)] = [n for n in nums if n is not None]
    return out


def parse_ranges(soup, title):
    """Parse a 'Compounded ... Growth' ranges-table into {'5 Years': 25.0, ...}."""
    out = {}
    for tbl in soup.select("table.ranges-table"):
        th = tbl.find("th")
        if th and title.lower() in th.get_text(strip=True).lower():
            for tr in tbl.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    label = tds[0].get_text(strip=True).replace(":", "")
                    out[label] = _to_float(tds[1].get_text())
            break
    return out


def _section_table(soup, sec_id):
    sec = soup.find(id=sec_id)
    return sec.find("table") if sec else None


def row_values(table, label):
    """Return the full list of cell floats for the first row matching `label`."""
    if not table:
        return []
    for tr in table.find_all("tr"):
        head = tr.find(["td", "th"])
        if head and label.lower() in head.get_text(strip=True).lower():
            cells = tr.find_all("td")[1:]
            return [_to_float(c.get_text()) for c in cells]
    return []


def parse_shareholding(soup):
    """Return (promoter_latest, promoter_earliest, pledge_latest) in %."""
    sec = soup.find(id="shareholding")
    if not sec:
        return None, None, None
    tbl = sec.find("table")
    prom = [v for v in row_values(tbl, "Promoter") if v is not None]
    pledge = [v for v in row_values(tbl, "Pledged") if v is not None]
    prom_latest = prom[-1] if prom else None
    prom_early = prom[0] if prom else None
    pledge_latest = pledge[-1] if pledge else None
    return prom_latest, prom_early, pledge_latest


def _cagr(series, years_each=1):
    """CAGR (%) of a clean numeric series from first to last positive value."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    n = (len(vals) - 1) * years_each
    return round(((vals[-1] / vals[0]) ** (1 / n) - 1) * 100, 1)


def _tail_sum(series, n=5):
    vals = [v for v in series if v is not None]
    return sum(vals[-n:]) if vals else None


def _last(series):
    vals = [v for v in series if v is not None]
    return vals[-1] if vals else None


# --------------------------------------------------------------------------- #
# Collect
# --------------------------------------------------------------------------- #
def collect(ticker):
    url, soup = fetch(ticker)
    if soup is None:
        return {"ticker": ticker, "url": url, "ok": False}

    top = parse_top_ratios(soup)

    def top_val(label):
        v = top.get(label)
        return v[0] if v else None

    sales = parse_ranges(soup, "Compounded Sales Growth")
    profit = parse_ranges(soup, "Compounded Profit Growth")

    pl = _section_table(soup, "profit-loss")
    bs = _section_table(soup, "balance-sheet")
    cf = _section_table(soup, "cash-flow")

    op_profit = row_values(pl, "Operating Profit")   # proxy for EBITDA
    net_profit_series = row_values(pl, "Net Profit")
    eps_series = row_values(pl, "EPS")
    interest_series = row_values(pl, "Interest")
    pbt_series = row_values(pl, "Profit before tax")
    equity_series = row_values(bs, "Equity Capital")  # share-count dilution proxy
    borrowings = _last(row_values(bs, "Borrowings"))
    reserves = _last(row_values(bs, "Reserves"))
    equity = _last(equity_series)
    cfo_series = row_values(cf, "Cash from Operating Activity")

    opm = _last(row_values(pl, "OPM %"))
    net_profit = _last(net_profit_series)
    cfo = _last(cfo_series)

    prom_latest, prom_early, pledge = parse_shareholding(soup)

    # Debt / equity (ex-financials proxy: borrowings / net worth)
    de = None
    if borrowings is not None and reserves is not None:
        denom = reserves + (equity or 0)
        de = round(borrowings / denom, 2) if denom else None

    # Cumulative 5y CFO / EBITDA (hard-flag input)
    cfo5 = _tail_sum(cfo_series, 5)
    ebitda5 = _tail_sum(op_profit, 5)
    cfo_ebitda = round(cfo5 / ebitda5, 2) if (cfo5 and ebitda5 and ebitda5 > 0) else None

    # CFO / Net profit (quality input)
    cfo_np = round(cfo / net_profit, 2) if (cfo and net_profit) else None

    # Interest coverage = EBIT / interest, EBIT = PBT + interest (latest year)
    pbt = _last(pbt_series)
    interest = _last(interest_series)
    int_cov = None
    if pbt is not None and interest:
        int_cov = round((pbt + interest) / interest, 1) if interest > 0 else None

    # Equity-dilution CAGR (share-count proxy via equity capital at constant FV)
    dilution = _cagr(equity_series)

    # EPS CAGR (earnings-per-share engine, dilution-adjusted)
    eps_cagr = _cagr(eps_series)

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
        "roe": top_val("ROE"),
        "opm": opm,
        "de": de,
        "int_cov": int_cov,
        "sales5y": sales.get("5 Years"),
        "sales3y": sales.get("3 Years"),
        "profit5y": profit.get("5 Years"),
        "profit3y": profit.get("3 Years"),
        "eps_cagr5y": eps_cagr,
        "prom_latest": prom_latest,
        "prom_early": prom_early,
        "pledge": pledge,
        "dilution": dilution,
        "cfo_np": cfo_np,
        "cfo_ebitda": cfo_ebitda,
    }


# --------------------------------------------------------------------------- #
# Hard red-flag forensic screen (run FIRST)
# --------------------------------------------------------------------------- #
# Each entry: (label, status, evidence)
#   status: "FAIL" (flag fires), "PASS" (checked, clean), "MANUAL" (needs AR)
def red_flag_screen(m):
    rows = []

    def add(label, fired, evidence):
        rows.append((label, "FAIL" if fired else "PASS", evidence))

    # 1. CFO/EBITDA < 50% cumulative 5y
    ce = m.get("cfo_ebitda")
    if ce is None:
        rows.append(("CFO/EBITDA ≥ 50% (5y cum.)", "MANUAL", "cash-flow data unavailable"))
    else:
        add("CFO/EBITDA ≥ 50% (5y cum.)", ce < 0.50, f"CFO/EBITDA = {int(ce*100)}%")

    # 2. Promoter pledging > 20%
    pl = m.get("pledge")
    if pl is None:
        rows.append(("Promoter pledge ≤ 20%", "MANUAL", "pledge % not disclosed in table"))
    else:
        add("Promoter pledge ≤ 20%", pl > 20, f"pledged {pl:.0f}%")

    # 3. Promoter stake < 40% OR falling > 3pp
    prom, prom0 = m.get("prom_latest"), m.get("prom_early")
    if prom is None:
        rows.append(("Promoter stake ≥ 40% & stable", "MANUAL", "shareholding unavailable"))
    else:
        low = prom < 40
        falling = prom0 is not None and (prom0 - prom) > 3
        ev = f"promoter {prom:.0f}%"
        if prom0 is not None:
            ev += f" (from {prom0:.0f}%)"
        add("Promoter stake ≥ 40% & stable", low or falling, ev)

    # 4. Equity dilution: share-count CAGR > 4%
    dil = m.get("dilution")
    if dil is None:
        rows.append(("Share count CAGR ≤ 4%", "MANUAL", "equity-capital history unavailable"))
    else:
        add("Share count CAGR ≤ 4%", dil > 4, f"equity-cap CAGR ≈ {dil}%")

    # 9. Debt: D/E > 1 OR interest coverage < 3x
    de, ic = m.get("de"), m.get("int_cov")
    fired_debt = (de is not None and de > 1) or (ic is not None and ic < 3)
    if de is None and ic is None:
        rows.append(("D/E ≤ 1 & interest cover ≥ 3×", "MANUAL", "balance-sheet data unavailable"))
    else:
        ev = []
        if de is not None:
            ev.append(f"D/E {de}")
        if ic is not None:
            ev.append(f"int.cover {ic}×")
        add("D/E ≤ 1 & interest cover ≥ 3×", fired_debt, ", ".join(ev))

    # 5-8, 10: require the annual report — flagged for manual review.
    rows.append(("Receivables not outpacing sales (>1.5×, 2y)", "MANUAL",
                 "needs AR / quarterly receivables — review manually"))
    rows.append(("No auditor resignation / small-auditor risk", "MANUAL",
                 "needs auditor's report — review manually"))
    rows.append(("Related-party transactions reasonable", "MANUAL",
                 "needs AR RPT schedule — review manually"))
    rows.append(("Contingent liabilities ≤ 25% of net worth", "MANUAL",
                 "needs AR contingent-liability note — review manually"))
    rows.append(("Promoter remuneration / SEBI history clean", "MANUAL",
                 "needs AR + SEBI orders — review manually"))

    hard_count = sum(1 for _, st, _ in rows if st == "FAIL")
    return rows, hard_count


# --------------------------------------------------------------------------- #
# Scoring (six pillars 0-100) — see references/scoring-rubric.md
# --------------------------------------------------------------------------- #
def _clamp(x):
    return int(max(0, min(100, round(x))))


def score(m):
    mcap = m.get("mcap")
    roce = m.get("roce")
    cfo_eb = m.get("cfo_ebitda")
    cfo_np = m.get("cfo_np")
    de = m.get("de")
    s5 = m.get("sales5y")
    p5 = m.get("profit5y")
    eps5 = m.get("eps_cagr5y")
    opm = m.get("opm")
    pe = m.get("pe")
    prom = m.get("prom_latest")
    prom0 = m.get("prom_early")
    pledge = m.get("pledge")
    dil = m.get("dilution")
    just = {}

    # S — Size & obscurity
    if mcap is None:
        s = 30
    elif mcap < 500:
        s = 55          # micro-cap: liquidity/governance risk caps the score
    elif mcap < 5000:
        s = 84
    elif mcap < 10000:
        s = 68
    elif mcap < 15000:
        s = 56
    elif mcap < 50000:
        s = 40
    else:
        s = 20
    just["S"] = f"Mcap ≈ Rs {fmt(mcap)} cr → re-rating room scored {s}."

    # Q — Quality
    if roce is None:
        q = 30
    elif roce >= 25:
        q = 82
    elif roce >= 20:
        q = 72
    elif roce >= 15:
        q = 58
    elif roce >= 12:
        q = 45
    else:
        q = 25
    qbits = [f"ROCE {fmt(roce, '%')}"]
    conv = cfo_eb if cfo_eb is not None else cfo_np
    if conv is not None:
        if conv >= 0.70:
            q += 8
        elif conv < 0.50:
            q -= 15
        qbits.append(f"CFO/EBITDA {int(conv*100)}%")
    if de is not None:
        if de < 0.3:
            q += 5
        elif de > 1:
            q -= 15
        qbits.append(f"D/E {de}")
    q = _clamp(q)
    just["Q"] = "; ".join(qbits) + f" → {q}."

    # G — Growth
    g_inputs = [v for v in (s5, eps5 if eps5 is not None else p5) if v is not None]
    if not g_inputs:
        g = 35
    else:
        avg = sum(g_inputs) / len(g_inputs)
        if avg >= 20:
            g = 82
        elif avg >= 15:
            g = 65
        elif avg >= 12:
            g = 52
        elif avg >= 10:
            g = 42
        else:
            g = 32
    earn5 = eps5 if eps5 is not None else p5
    if earn5 is not None and earn5 < 8:     # stalled earnings cap
        g = min(g, 45)
    g = _clamp(g)
    just["G"] = (f"5y Sales {fmt(s5, '%')}, EPS/PAT {fmt(earn5, '%')} → {g} "
                 "(stalled earnings capped at 45).")

    # L — Longevity / moat (numeric proxy: margin + return durability)
    if roce is None:
        l = 45
    elif (opm or 0) >= 25 and roce >= 20:
        l = 72
    elif (opm or 0) >= 18 and roce >= 18:
        l = 65
    elif roce >= 15:
        l = 55
    else:
        l = 42
    just["L"] = (f"OPM {fmt(opm, '%')}, ROCE {fmt(roce, '%')} → {l} "
                 "(numeric proxy; confirm moat in AR/concall).")

    # M — Management & capital allocation
    if prom is None:
        mgmt = 45
    elif prom >= 50:
        mgmt = 75
    elif prom >= 40:
        mgmt = 55
    else:
        mgmt = 35
    mbits = [f"Promoter {fmt(prom, '%')}"]
    if prom is not None and prom0 is not None and (prom0 - prom) > 3:
        mgmt = min(mgmt, 50) - 10            # falling promoter holding
        mbits.append(f"falling from {prom0:.0f}%")
    if pledge is not None and pledge > 20:
        mgmt -= 20
        mbits.append(f"pledge {pledge:.0f}%")
    if dil is not None and dil > 4:
        mgmt -= 10
        mbits.append(f"dilution {dil}%")
    if de is not None and de > 1:
        mgmt -= 5
    mgmt = _clamp(mgmt)
    just["M"] = "; ".join(mbits) + f" → {mgmt} (candor needs AR/concall review)."

    # P — Price (PEG vs 5y earnings growth)
    growth_for_peg = earn5 if (earn5 and earn5 > 0) else s5
    if pe is None or not growth_for_peg or growth_for_peg <= 0:
        p = 35
        peg = None
    else:
        peg = round(pe / growth_for_peg, 2)
        if pe > 60 and growth_for_peg < 25:
            p = 18
        elif peg <= 1:
            p = 82
        elif peg <= 1.5:
            p = 62
        elif peg <= 2.5:
            p = 40
        else:
            p = 22
    just["P"] = f"P/E {fmt(pe)}, PEG {fmt(peg)} → {p}."

    pillars = {"S": s, "Q": q, "G": g, "L": l, "M": mgmt, "P": p}
    total = round(sum(pillars[k] * WEIGHTS[k] for k in WEIGHTS), 1)
    return pillars, total, just


def classify(total, hard_count):
    if hard_count >= 2:
        return "AVOID"
    if total >= 75:
        return "Strong candidate"
    if total >= 60:
        return "Watchlist"
    if total >= 45:
        return "Unproven"
    return "Pass"


# --------------------------------------------------------------------------- #
# Multibagger math: base / bull / bear EPS CAGR x exit P/E -> implied multiple
# --------------------------------------------------------------------------- #
def multibagger_math(m):
    pe = m.get("pe")
    earn5 = m.get("eps_cagr5y")
    if earn5 is None:
        earn5 = m.get("profit5y")
    if pe is None or pe <= 0 or earn5 is None:
        return None

    base_g = max(0.0, min(earn5, 30.0))      # don't extrapolate insane growth
    bear_g = max(0.0, base_g * 0.5)
    bull_g = min(base_g * 1.3, 35.0)

    # Exit P/E assumptions: bear de-rates, base holds, bull modestly re-rates.
    scenarios = [
        ("Bear", bear_g, max(pe * 0.6, 8)),
        ("Base", base_g, pe),
        ("Bull", bull_g, min(pe * 1.5, 45)),
    ]
    out = []
    for name, g, exit_pe in scenarios:
        rerate = exit_pe / pe
        x5 = round((1 + g / 100) ** 5 * rerate, 1)
        x10 = round((1 + g / 100) ** 10 * rerate, 1)
        out.append((name, round(g, 1), round(exit_pe, 1), x5, x10))
    return out


def kill_switches(m, hard_rows):
    """Explicit thesis kill-switches for this name."""
    ks = [
        "Exit if promoter pledging appears, or pledge rises above 20%.",
        "Exit if cumulative CFO/EBITDA drops below 60%.",
        "Exit if promoter holding falls > 3pp in a year without a stated reason.",
        "Exit if 5y EPS/PAT CAGR stalls below 8% or margins compress structurally.",
        "Re-underwrite if D/E rises above 1 or interest coverage falls below 3×.",
    ]
    # Add a manual-diligence reminder for the flags code cannot see.
    manual = [lbl for lbl, st, _ in hard_rows if st == "MANUAL"]
    if manual:
        ks.append("Before acting, manually clear: " + "; ".join(manual[:5]) + ".")
    return ks


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def fmt(v, suffix=""):
    return f"{v:g}{suffix}" if isinstance(v, (int, float)) else "n/a"


def _thesis(m):
    parts = []
    if m.get("roce") is not None:
        parts.append(f"ROCE {fmt(m['roce'],'%')}")
    g = m.get("eps_cagr5y") if m.get("eps_cagr5y") is not None else m.get("profit5y")
    if g is not None:
        parts.append(f"{fmt(g,'%')} earnings CAGR")
    if m.get("mcap") is not None:
        parts.append(f"Rs {fmt(m['mcap'])} cr mcap")
    return ", ".join(parts) if parts else "limited data"


def _top_risk(m, hard_rows):
    fired = [lbl for lbl, st, _ in hard_rows if st == "FAIL"]
    if fired:
        return "Hard flag: " + fired[0]
    if (m.get("pe") or 0) > 50:
        return f"Rich valuation (P/E {fmt(m['pe'])}) — re-rating engine already spent."
    if m.get("eps_cagr5y") is not None and m["eps_cagr5y"] < 12:
        return "Growth engine modest — needs an inflection to compound."
    return "Small-cap base rate: most underperform; verify filings independently."


def build_report(rows, today):
    L = []
    L.append("# Multibagger Hunter (India — NSE/BSE) — Automated Daily Scan\n")
    L.append(f"**Date:** {today} · **Framework:** SQGLP / six-pillar "
             "(quantitative proxy) · **Source:** screener.in\n")
    L.append("> Analytical research, **NOT investment advice**. This scores "
             "*numbers only* — it does not read annual reports or concalls, so "
             "Longevity (moat), Management candor, and red flags 5–8/10 are "
             "heuristic proxies flagged for manual review. Verify filings "
             "independently and size positions accordingly: multibagger hunting "
             "means accepting that many picks fail — portfolio construction "
             "matters as much as selection. Most small caps underperform.\n")

    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]

    # ----- Ranked comparison ------------------------------------------------ #
    L.append("## Ranked comparison\n")
    L.append("| Rank | Company | Ticker | Mcap (Rs cr) | S | Q | G | L | M | P | "
             "**Total** | Hard flags | Verdict |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(ok, 1):
        p = r["pillars"]
        L.append(
            f"| {i} | {r['name']} | {r['ticker']} | {fmt(r['mcap'])} | "
            f"{p['S']} | {p['Q']} | {p['G']} | {p['L']} | {p['M']} | {p['P']} | "
            f"**{r['total']}** | {r['hard_count']} | {r['verdict']} |")
    for r in bad:
        L.append(f"| — | {r['ticker']} | {r['ticker']} | n/a | — | — | — | — | — | "
                 "— | n/a | n/a | DATA UNAVAILABLE |")

    L.append("\n*Weights: S 15% · Q 25% · G 20% · L 15% · M 15% · P 10%. "
             "Bands: ≥75 Strong · 60–74 Watchlist · 45–59 Unproven · <45 Pass · "
             "2+ hard flags = AVOID.*\n")

    # ----- Per-stock detail ------------------------------------------------- #
    L.append("---\n\n## Per-company detail\n")
    for r in ok:
        L.extend(_stock_section(r))

    if bad:
        L.append("### Data unavailable\n")
        for r in bad:
            L.append(f"- **{r['ticker']}** — screener.in fetch/parse failed "
                     f"([page]({r['url']})). Check the ticker code.")
        L.append("")

    L.append("---\n")
    L.append("*Hard red flags screened: CFO/EBITDA < 50% (5y), promoter pledge "
             "> 20%, promoter stake < 40% or falling > 3pp, share-count CAGR "
             "> 4%, D/E > 1 or interest cover < 3×. Receivables, auditor, "
             "related-party, contingent-liability and remuneration/SEBI checks "
             "need the annual report and are flagged for manual review. "
             "Not investment advice.*")
    return "\n".join(L)


def _stock_section(r):
    m = r
    p = r["pillars"]
    just = r["justify"]
    L = []
    L.append(f"### {r['name']} ({r['ticker']})\n")

    # Verdict box
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| **Score** | **{r['total']} / 100** |")
    L.append(f"| **Classification** | {r['verdict']} |")
    L.append(f"| **Thesis** | {_thesis(m)} |")
    L.append(f"| **Top risk** | {_top_risk(m, r['flags'])} |")
    L.append(f"| **Hard flags** | {r['hard_count']} fired |")
    L.append("")

    # Red-flag table
    L.append("**Red-flag forensic screen** (run first):\n")
    L.append("| Check | Result | Evidence |")
    L.append("|---|---|---|")
    icon = {"FAIL": "🔴 FAIL", "PASS": "🟢 pass", "MANUAL": "🟡 manual"}
    for lbl, st, ev in r["flags"]:
        L.append(f"| {lbl} | {icon[st]} | {ev} |")
    L.append("")

    # Pillar scorecard
    L.append("**Pillar scorecard:**\n")
    L.append("| Pillar | Weight | Score | Justification |")
    L.append("|---|---|---|---|")
    for k in ("S", "Q", "G", "L", "M", "P"):
        L.append(f"| {k} — {PILLAR_NAMES[k]} | {int(WEIGHTS[k]*100)}% | "
                 f"{p[k]} | {just[k]} |")
    L.append(f"| **Total** | 100% | **{r['total']}** | weighted sum |")
    L.append("")

    # Multibagger math
    mm = r.get("math")
    if mm:
        L.append("**Multibagger math** (implied price multiple = EPS growth ^ n × "
                 "exit-P/E re-rating):\n")
        L.append("| Scenario | EPS CAGR | Exit P/E | 5y multiple | 10y multiple |")
        L.append("|---|---|---|---|---|")
        for name, g, exit_pe, x5, x10 in mm:
            L.append(f"| {name} | {g}% | {exit_pe}x | {x5}x | {x10}x |")
        L.append(f"\n*From current P/E {fmt(m['pe'])} and ~{fmt(m.get('eps_cagr5y') or m.get('profit5y'),'%')} "
                 "historical earnings CAGR. Forward-looking and illustrative only.*\n")
    else:
        L.append("*Multibagger math: insufficient P/E or growth data.*\n")

    # Kill switches
    L.append("**What would change the thesis (kill-switches):**\n")
    for k in r["kill"]:
        L.append(f"- {k}")
    L.append("")

    # Sources
    L.append(f"**Sources:** [screener.in/{r['ticker']}]({r['url']}) · "
             f"verify on [NSE](https://www.nseindia.com), "
             f"[BSE announcements](https://www.bseindia.com/corporates/ann.html), "
             f"and CRISIL/ICRA/CARE rating rationales.\n")
    L.append("")
    return L


def build_email_html(rows, today):
    head = (
        '<div style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:820px">'
        f'<h2>Multibagger Hunter — Daily Scan ({today})</h2>'
        '<p style="background:#fff8e1;border-left:4px solid #f0ad4e;padding:8px 12px;'
        'font-size:13px">Quantitative proxy of the SQGLP six-pillar framework. '
        '<b>Not investment advice.</b> Longevity, management candor and several '
        'red flags need manual annual-report review.</p>'
        '<table cellpadding="6" cellspacing="0" border="1" '
        'style="border-collapse:collapse;font-size:13px">'
        '<tr style="background:#1f3a5f;color:#fff"><th>#</th><th>Company</th>'
        '<th>Ticker</th><th>Mcap</th><th>Score</th><th>Flags</th>'
        '<th>Verdict</th></tr>'
    )
    body = ""
    for i, r in enumerate([x for x in rows if x.get("ok")], 1):
        bg = "#e8f5e9" if r["verdict"] == "Strong candidate" else (
            "#fdecea" if r["verdict"] == "AVOID" else "#ffffff")
        body += (f'<tr style="background:{bg}"><td>{i}</td><td>{r["name"]}</td>'
                 f'<td>{r["ticker"]}</td><td>{fmt(r["mcap"])}</td>'
                 f'<td><b>{r["total"]}</b></td><td>{r["hard_count"]}</td>'
                 f'<td>{r["verdict"]}</td></tr>')
    tail = ('</table><p style="font-size:12px;color:#555">Full report with '
            'red-flag tables, pillar scorecards and multibagger math is committed '
            'to <code>reports/latest.md</code> in your repo. '
            'Verify filings independently; size positions accordingly.</p></div>')
    return head + body + tail


def send_email(subject, html):
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        print("Email not configured (SMTP_USER/SMTP_PASS/EMAIL_TO) — skipping.")
        return
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    print(f"Email sent to {to}")


# --------------------------------------------------------------------------- #
def load_watchlist():
    path = os.path.join(os.path.dirname(__file__), "watchlist.txt")
    if os.path.exists(path):
        with open(path) as f:
            names = [ln.strip().upper() for ln in f if ln.strip()
                     and not ln.startswith("#")]
        if names:
            return names
    return DEFAULT_WATCHLIST


def main():
    today = datetime.date.today().isoformat()
    watchlist = load_watchlist()
    print(f"Scanning {len(watchlist)} tickers: {', '.join(watchlist)}")

    rows = []
    for t in watchlist:
        print(f"  fetching {t} ...")
        m = collect(t)
        if m.get("ok"):
            m["flags"], m["hard_count"] = red_flag_screen(m)
            m["pillars"], m["total"], m["justify"] = score(m)
            m["verdict"] = classify(m["total"], m["hard_count"])
            m["math"] = multibagger_math(m)
            m["kill"] = kill_switches(m, m["flags"])
        rows.append(m)

    ok_rows = [r for r in rows if r.get("ok")]
    bad_rows = [r for r in rows if not r.get("ok")]
    # AVOID names sink to the bottom regardless of raw score.
    ok_rows.sort(key=lambda r: (r["verdict"] == "AVOID", -r["total"]))
    rows = ok_rows + bad_rows

    report = build_report(rows, today)

    reports_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    dated = os.path.join(reports_dir, f"scan-{today}.md")
    latest = os.path.join(reports_dir, "latest.md")
    for path in (dated, latest):
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    print(f"Wrote {dated}")

    if ok_rows:
        top = ok_rows[0]
        subject = (f"Multibagger Scan ({today}): {top['name']} tops "
                   f"@ {top['total']} ({top['verdict']})")
        send_email(subject, build_email_html(rows, today))
    else:
        print("No tickers parsed successfully — check screener.in availability.")
        sys.exit(1)


if __name__ == "__main__":
    main()
