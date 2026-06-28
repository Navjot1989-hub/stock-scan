#!/usr/bin/env python3
"""
Multibagger Hunter — automated scan.

Scrapes screener.in for a watchlist of Indian (NSE/BSE) small/midcaps, runs a
red-flag screen, scores six pillars (Size, Quality, Growth, Longevity,
Management, Price) as code, and writes a ranked Markdown report. Optionally
emails the summary via Gmail SMTP when SMTP_USER / SMTP_PASS / EMAIL_TO are set.

This is a QUANTITATIVE proxy of the SQGLP framework. It reads numbers only — it
does NOT read annual reports or concalls, so the Longevity (moat) and the candor
part of Management are heuristic proxies and deserve human review.

Analytical research, NOT investment advice.
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

# Default watchlist (screener.in codes). Override by creating watchlist.txt
# with one ticker per line.
DEFAULT_WATCHLIST = [
    "TIPSMUSIC", "CAPLIPOINT", "GRAVITA", "NEWGEN", "ANANDRATHI",
    "GARFIBRES", "CONTROLPR", "PIXTRANS", "RACLGEAR", "SUPRAJIT",
]

WEIGHTS = {"S": 0.15, "Q": 0.25, "G": 0.20, "L": 0.15, "M": 0.15, "P": 0.10}


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
    """Return the list of cell floats for the first row matching `label`."""
    if not table:
        return []
    for tr in table.find_all("tr"):
        head = tr.find(["td", "th"])
        if head and label.lower() in head.get_text(strip=True).lower():
            cells = tr.find_all("td")[1:]
            return [_to_float(c.get_text()) for c in cells]
    return []


def parse_shareholding(soup):
    """Return (promoter_latest, promoter_earliest) in percent, or (None, None)."""
    sec = soup.find(id="shareholding")
    if not sec:
        return None, None
    tbl = sec.find("table")
    vals = row_values(tbl, "Promoter")
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    return vals[-1], vals[0]


# --------------------------------------------------------------------------- #
# Metrics + scoring
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

    def last(vals):
        vals = [v for v in vals if v is not None]
        return vals[-1] if vals else None

    opm = last(row_values(pl, "OPM %"))
    net_profit = last(row_values(pl, "Net Profit"))
    borrowings = last(row_values(bs, "Borrowings"))
    reserves = last(row_values(bs, "Reserves"))
    equity = last(row_values(bs, "Equity Capital"))
    cfo = last(row_values(cf, "Cash from Operating Activity"))

    prom_latest, prom_early = parse_shareholding(soup)

    de = None
    if borrowings is not None and reserves is not None:
        denom = reserves + (equity or 0)
        de = round(borrowings / denom, 2) if denom else None

    cfo_np = round(cfo / net_profit, 2) if (cfo and net_profit) else None

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
        "sales5y": sales.get("5 Years"),
        "sales3y": sales.get("3 Years"),
        "profit5y": profit.get("5 Years"),
        "profit3y": profit.get("3 Years"),
        "prom_latest": prom_latest,
        "prom_early": prom_early,
        "cfo_np": cfo_np,
    }


def _clamp(x):
    return max(0, min(100, x))


def score(m):
    """Score the six pillars 0-100 from the parsed numbers. Conservative:
    missing data scores the lower band."""
    mcap = m.get("mcap")
    roce = m.get("roce")
    cfo_np = m.get("cfo_np")
    de = m.get("de")
    s5 = m.get("sales5y")
    p5 = m.get("profit5y")
    opm = m.get("opm")
    pe = m.get("pe")
    prom = m.get("prom_latest")
    prom0 = m.get("prom_early")

    # S — Size & obscurity
    if mcap is None:
        s = 30
    elif mcap < 500:
        s = 55          # micro-cap: governance/liquidity risk caps the score
    elif mcap < 5000:
        s = 82
    elif mcap < 15000:
        s = 62
    elif mcap < 50000:
        s = 40
    else:
        s = 20

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
    if cfo_np is not None:
        if cfo_np >= 0.70:
            q += 8
        elif cfo_np < 0.50:
            q -= 15
    if de is not None:
        if de < 0.3:
            q += 5
        elif de > 1:
            q -= 15
    q = _clamp(q)

    # G — Growth
    g_inputs = [v for v in (s5, p5) if v is not None]
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
    if p5 is not None and p5 < 8:      # stalled earnings cap
        g = min(g, 45)
    g = _clamp(g)

    # L — Longevity / moat (PROXY: margin level + return quality)
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

    # M — Management & capital allocation
    if prom is None:
        mgmt = 45
    elif prom >= 50:
        mgmt = 75
    elif prom >= 40:
        mgmt = 55
    else:
        mgmt = 35
    if prom is not None and prom0 is not None and (prom0 - prom) > 3:
        mgmt = min(mgmt, 50) - 10        # falling promoter holding
    if de is not None and de > 1:
        mgmt -= 5
    mgmt = _clamp(mgmt)

    # P — Price (PEG vs 5y profit growth)
    growth_for_peg = p5 if (p5 and p5 > 0) else s5
    if pe is None or not growth_for_peg or growth_for_peg <= 0:
        p = 35
    else:
        peg = pe / growth_for_peg
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

    pillars = {"S": s, "Q": q, "G": g, "L": l, "M": mgmt, "P": p}
    total = round(sum(pillars[k] * WEIGHTS[k] for k in WEIGHTS), 1)
    return pillars, total


def red_flags(m):
    """Return (hard_flags, soft_flags) as lists of strings."""
    hard, soft = [], []
    if m.get("cfo_np") is not None and m["cfo_np"] < 0.5:
        hard.append(f"CFO/NP {int(m['cfo_np']*100)}% (<50%)")
    if m.get("prom_latest") is not None and m["prom_latest"] < 40:
        hard.append(f"Promoter {m['prom_latest']:.0f}% (<40%)")
    if (m.get("prom_latest") is not None and m.get("prom_early") is not None
            and (m["prom_early"] - m["prom_latest"]) > 3):
        hard.append(
            f"Promoter holding falling ({m['prom_early']:.0f}%→{m['prom_latest']:.0f}%)")
    if m.get("de") is not None and m["de"] > 1:
        hard.append(f"D/E {m['de']} (>1)")
    if m.get("profit5y") is not None and m["profit5y"] < 8:
        soft.append(f"Stalled earnings (5y PAT CAGR {m['profit5y']:.0f}%)")
    return hard, soft


def classify(total, hard):
    if len(hard) >= 2:
        return "AVOID"
    if total >= 75:
        return "Strong candidate"
    if total >= 60:
        return "Watchlist"
    if total >= 45:
        return "Unproven"
    return "Pass"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def fmt(v, suffix=""):
    return f"{v:g}{suffix}" if isinstance(v, (int, float)) else "n/a"


def build_report(rows, today):
    lines = []
    lines.append("# Multibagger Hunter — Automated Scan\n")
    lines.append(f"**Date:** {today} · **Framework:** SQGLP / six-pillar "
                 "(quantitative proxy) · **Source:** screener.in\n")
    lines.append("> Analytical research, NOT investment advice. Longevity (moat) "
                 "and management candor are heuristic proxies from numbers only — "
                 "verify with annual reports and concalls.\n")

    lines.append("## Ranked verdict\n")
    lines.append("| Rank | Company | Ticker | Mcap (Rs cr) | Score | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        if not r.get("ok"):
            lines.append(f"| — | {r['ticker']} | {r['ticker']} | n/a | n/a | "
                         "DATA UNAVAILABLE |")
            continue
        lines.append(
            f"| {i} | {r['name']} | {r['ticker']} | {fmt(r['mcap'])} | "
            f"**{r['total']}** | {r['verdict']} |")

    lines.append("\n## Pillar scorecard & flags\n")
    lines.append("| Ticker | S | Q | G | L | M | P | Total | ROCE | 5y Sales | "
                 "5y PAT | Prom % | Hard flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if not r.get("ok"):
            continue
        p = r["pillars"]
        flags = "; ".join(r["hard"]) or "—"
        lines.append(
            f"| {r['ticker']} | {p['S']} | {p['Q']} | {p['G']} | {p['L']} | "
            f"{p['M']} | {p['P']} | **{r['total']}** | {fmt(r['roce'],'%')} | "
            f"{fmt(r['sales5y'],'%')} | {fmt(r['profit5y'],'%')} | "
            f"{fmt(r['prom_latest'],'%')} | {flags} |")

    lines.append("\n## Sources\n")
    for r in rows:
        lines.append(f"- [{r['ticker']}]({r['url']})")
    lines.append("\n*Weights: S 15% · Q 25% · G 20% · L 15% · M 15% · P 10%. "
                 "Verdict bands: ≥75 Strong · 60–74 Watchlist · 45–59 Unproven · "
                 "<45 Pass · 2+ hard flags = AVOID. Not investment advice.*")
    return "\n".join(lines)


def build_email_html(rows, today):
    head = (
        '<div style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:760px">'
        f'<h2>Multibagger Hunter — Automated Scan ({today})</h2>'
        '<p style="background:#fff8e1;border-left:4px solid #f0ad4e;padding:8px 12px;'
        'font-size:13px">Quantitative proxy of the SQGLP framework. '
        '<b>Not investment advice.</b></p>'
        '<table cellpadding="6" cellspacing="0" border="1" '
        'style="border-collapse:collapse;font-size:13px">'
        '<tr style="background:#1f3a5f;color:#fff"><th>#</th><th>Company</th>'
        '<th>Ticker</th><th>Mcap</th><th>Score</th><th>Verdict</th></tr>'
    )
    body = ""
    for i, r in enumerate(rows, 1):
        if not r.get("ok"):
            continue
        body += (f"<tr><td>{i}</td><td>{r['name']}</td><td>{r['ticker']}</td>"
                 f"<td>{fmt(r['mcap'])}</td><td><b>{r['total']}</b></td>"
                 f"<td>{r['verdict']}</td></tr>")
    return head + body + "</table></div>"


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
            m["pillars"], m["total"] = score(m)
            m["hard"], m["soft"] = red_flags(m)
            m["verdict"] = classify(m["total"], m["hard"])
        rows.append(m)

    ok_rows = [r for r in rows if r.get("ok")]
    bad_rows = [r for r in rows if not r.get("ok")]
    ok_rows.sort(key=lambda r: r["total"], reverse=True)
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
                   f"@ {top['total']}")
        send_email(subject, build_email_html(rows, today))
    else:
        print("No tickers parsed successfully — check screener.in availability.")
        sys.exit(1)


if __name__ == "__main__":
    main()
