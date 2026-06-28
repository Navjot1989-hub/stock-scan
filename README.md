# Stock Scan — Multibagger Hunter (automated)

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
