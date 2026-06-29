# Stock Scan — automated Indian small/mid-cap screeners

Two independent automated screens of Indian (NSE/BSE) small/mid-caps. Both scrape
[screener.in](https://www.screener.in), score names as code, and write ranked
Markdown reports. Both run on **GitHub Actions on a schedule**, so they fire on
GitHub's servers **even when your own machine is off**.

| Scanner | File | What it finds |
|---|---|---|
| **Multibagger Hunter** | [`scan.py`](scan.py) | A watchlist scored on the **SQGLP / six-pillar** framework (Size, Quality, Growth, Longevity, Management, Price). |
| **Turnaround Hunter** | [`turnaround.py`](turnaround.py) | The whole small/mid-cap universe filtered to names **near their 52-week low with improving EBITDA** — see [below](#turnaround-hunter--52-week-low-recovery-scan). |

---

## Multibagger Hunter

Automated screen of Indian (NSE/BSE) small/midcaps for multibagger potential,
based on the **SQGLP / six-pillar** framework (Size, Quality, Growth, Longevity,
Management, Price). It scrapes [screener.in](https://www.screener.in), runs a
**hard red-flag forensic screen first** (2+ hard flags ⇒ AVOID), scores each
survivor as code against [`references/scoring-rubric.md`](references/scoring-rubric.md),
and writes a ranked Markdown report with per-company **verdict box, red-flag
table, pillar scorecard, multibagger-math table, kill-switches and sources**.

It runs on **GitHub Actions on a daily schedule (11:00 IST)**, so it fires on
GitHub's servers and emails you **even when your own machine is off**.

> ⚠️ Analytical research, **not investment advice**. This scores *numbers only* —
> it does not read annual reports or concalls, so the Longevity (moat) pillar and
> the candor part of Management are heuristic proxies. Verify filings yourself and
> size positions accordingly; most small caps underperform.

## What it produces

- `reports/scan-YYYY-MM-DD.md` — dated report, committed back to the repo each run
- `reports/latest.md` — always the most recent scan
- An optional email summary (see below)

## How it runs

`.github/workflows/scan.yml`:
- **Schedule:** `cron: "30 5 * * *"` → 05:30 UTC = **11:00 IST, every day**
- **Manual:** the **Run workflow** button on the repo's **Actions** tab runs it on demand

GitHub Actions cron is in **UTC**; adjust the cron line if you want a different IST
time. Scheduled runs can lag a few minutes under GitHub load — that is normal.

## Configure the watchlist

Edit [`watchlist.txt`](watchlist.txt) — one screener.in ticker per line. Commit and
the next run picks it up.

## Optional: email the results

The scheduled run can email you via Gmail SMTP. It only emails if all three repo
**secrets** exist (Settings → Secrets and variables → Actions → New repository secret):

| Secret | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Gmail **App Password** (not your normal password) — create at <https://myaccount.google.com/apppasswords> with 2-Step Verification on |
| `EMAIL_TO` | where to send (e.g. your own address) |

Without these secrets the workflow simply skips email and just commits the report.

## Run locally

```bash
pip install -r requirements.txt
python scan.py
# optional email:  SMTP_USER=... SMTP_PASS=... EMAIL_TO=... python scan.py
```

## Scoring (summary)

| Pillar | Weight | Coded test |
|---|---|---|
| S — Size | 15% | Market cap band (Rs 500–5,000 cr best; >50,000 cr penalised) |
| Q — Quality | 25% | ROCE band, ± CFO/Net-Profit conversion, ± debt/equity |
| G — Growth | 20% | 5y sales & profit CAGR; stalled earnings (<8%) capped |
| L — Longevity | 15% | Proxy: operating-margin level + return quality |
| M — Management | 15% | Promoter holding level & trend, debt; falling stake penalised |
| P — Price | 10% | PEG vs 5y profit growth; >60x P/E on low growth penalised |

Total = .15·S + .25·Q + .20·G + .15·L + .15·M + .10·P.
Verdict: **≥75** Strong · **60–74** Watchlist · **45–59** Unproven · **<45** Pass ·
**2+ hard red flags = AVOID**.

**Hard red flags screened in code:** CFO/EBITDA < 50% (5y cumulative), promoter
pledge > 20%, promoter stake < 40% or falling > 3pp, share-count CAGR > 4%,
D/E > 1 or interest coverage < 3×. Receivables-vs-sales, auditor, related-party,
contingent-liability and remuneration/SEBI checks need the annual report and are
listed as **manual-review** items in each report. See the full framework and
hard-flag list in [`references/scoring-rubric.md`](references/scoring-rubric.md).

---

## Turnaround Hunter — 52-week-low recovery scan

Unlike the watchlist-driven multibagger scan, this one sweeps the **whole
small/mid-cap universe** and surfaces **beaten-down turnarounds**: stocks trading
**near their 52-week low** where the **last four quarters' EBITDA (Operating
Profit) is healthy and improving** — plus an overlay for sectors you believe are
turning around on domestic + global factors.

> ⚠️ Analytical research, **not investment advice**. EBITDA is approximated by
> screener's *Operating Profit* line; "improving" and "sector turnaround" are
> heuristics from numbers + an editable overlay. Most stocks near 52-week lows are
> there for good reasons — verify concalls and filings before acting.

### How it works

1. **Universe** — queries screener.in's *screen* with the market-cap band and a
   "operating profit positive **and** improving year-on-year" filter (see
   [`screen_query.txt`](screen_query.txt)). This narrows the universe server-side.
2. **Per-company analysis** — for each candidate it reads the company page and
   computes: % above the 52-week low, the last four quarters of Operating Profit /
   OPM / Sales, YoY and QoQ EBITDA change, trough-to-latest recovery, and the sector.
3. **Gating filter** — a name only makes the report if **all** of:
   - market cap in **Rs 500–75,000 cr** (small + mid),
   - price **within 15% of its 52-week low**,
   - **all four** of the last quarters have **positive** Operating Profit,
   - the latest quarter is **improving** (EBITDA up YoY, or the best of the last
     four and above the prior quarter).
4. **Score & rank (0–100)** = 30% proximity-to-low + 28% EBITDA YoY + 24%
   trough-recovery + 18% OPM expansion, **+6** if the sector is in your turnaround
   overlay. Verdict: **≥70** Strong setup · **55–69** Watch · **45–54** Early · **<45** Pass.

### Configure it

| File | What to edit |
|---|---|
| [`screen_query.txt`](screen_query.txt) | The screener.in screen query defining the universe. Split across lines for readability; `#` comments ignored. If screener renames a field and the screen returns nothing, the scan falls back to `watchlist.txt` — fix the field names here. |
| [`sectors.json`](sectors.json) | Your **editable macro assumption**: which sectors are turning around, with a one-line domestic + global rationale. Matched as a case-insensitive substring against each company's industry; adds a score bonus + a note. Set `turnaround_sectors` to `{}` to disable. |

Thresholds can also be overridden via environment variables (defaults in parentheses):
`MCAP_MIN` (500), `MCAP_MAX` (75000), `NEAR_LOW_PCT` (15), `MAX_PAGES` (10),
`PAGE_DELAY` (0.7s). Uncomment them in
[`.github/workflows/turnaround.yml`](.github/workflows/turnaround.yml) to change a run.

### What it produces

- `reports/turnaround-YYYY-MM-DD.md` — dated report, committed back each run
- `reports/turnaround-latest.md` — always the most recent turnaround scan
- An optional email summary (reuses the **same** `SMTP_USER` / `SMTP_PASS` /
  `EMAIL_TO` secrets as the multibagger scan — see [above](#optional-email-the-results))

### How it runs

[`.github/workflows/turnaround.yml`](.github/workflows/turnaround.yml):
- **Schedule:** `cron: "0 3 1-31/2 * *"` → 03:00 UTC = **08:30 IST every alternate
  day** (offset 30 min from the multibagger scan so the two don't race to commit)
- **Manual:** the **Run workflow** button on the **Actions** tab runs it on demand

```bash
# Run locally (needs working HTTPS to screener.in):
python turnaround.py
```
