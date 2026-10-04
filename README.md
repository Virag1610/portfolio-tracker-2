# Folio Lab: my model portfolio tracker

A dark, dashboard-style website for tracking model portfolios of NSE/BSE stocks against a benchmark (Nifty Microcap 250 by default). Everything is free: the website is one HTML file hosted on GitHub Pages, and prices are downloaded by a free GitHub scheduled job.

## What it does

- **Dashboard** opens by default: your portfolio vs Nifty Microcap 250 chart, XIRR at the top, value, gain, cash, costs, worst fall from peak.
- **Several portfolios** (3, 4 or more), each with its own strategy notes and benchmark.
- **Add money / withdraw money** on any date. XIRR uses the exact dates, the same way Excel's XIRR does.
- **Transactions report** like a broker's: date, security name with NSE/BSE code, quantity, rate, value (quantity × rate), slippage, charges (with breakdown), net amount, effective price, buy cost, profit/loss, return %, holding period, cash balance after. Downloadable as CSV for Excel.
- **Holdings** with average cost, last close, day change, profit/loss, weight, days held.
- **Compare portfolios** on one chart and one table.
- **Dividends, bonus shares and stock splits** can be recorded so quantities and returns stay correct.
- Prices update automatically every trading day evening. You never type prices except your own buy/sell rate.

## Where the prices come from (official sources only)

| Data | Source |
|---|---|
| NSE closing prices | NSE daily equity bhavcopy (`nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_...csv.zip`) |
| BSE closing prices | BSE daily equity bhavcopy (`bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_...CSV`) |
| Nifty Microcap 250 and other indices | NSE daily index closing file (`nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv`) |

The job runs at about **5:00 PM IST** on weekdays, then again at 7:00 PM and 10:00 PM in case the exchange publishes its files late, plus a 9:00 AM catch-up the next morning.

Each run downloads **only the latest trading session** (one NSE file, one BSE file, one index file), never a range of past days. Every request has a 10-second timeout and the whole run finishes in under 15 seconds. It stores the latest close of **every** NSE and BSE listed share (BSE by scrip code such as `544342` and by ticker), so any company, nano cap or large cap, is valued straight away. A daily price point is also added for the stocks you hold and for the indices, so the chart history builds up from the day you start.

**Older history for a stock (one-time back-fill):** when you record a trade dated before the stored price history, the next run makes ONE request to Yahoo Finance's daily price history for that stock (`SYMBOL.NS` for NSE, `scripcode.BO` for BSE, e.g. `544342.BO`) and fills every missing day from your earliest trade date. Yahoo is not an official exchange source, so: official NSE/BSE closes are never overwritten; Yahoo's prices are first compared with the official closes already stored and are rejected if they differ by more than 1%; Yahoo's split- and bonus-adjusted prices are converted back to the actual traded prices; and each filled date is listed in `prices.json` (`yahoo_dates`). Once a stock is filled it is never fetched again. To compare Yahoo with the official closes for any stock yourself: **Actions → Update prices → Run workflow**, and type e.g. `BSE:544342` in the "check" box.

The benchmark is the **price index** (as published in NSE's daily file), so index dividends are not included.

## Costs used (equity delivery)

| Item | Default | Basis |
|---|---|---|
| STT | 0.1% on buy and on sell | Securities Transaction Tax, delivery |
| Stamp duty | 0.015% on buy | Indian Stamp Act rate for delivery |
| NSE transaction charge | 0.00297% | NSE, effective 1 Oct 2024 |
| BSE transaction charge | 0.00375% | BSE group A/B, effective 1 Oct 2024 |
| SEBI fee | ₹10 per crore | SEBI |
| GST | 18% on brokerage + transaction + SEBI charges | GST |
| Depository charge | ₹13 + GST per stock per sell day | Zerodha rate (varies by broker) |
| Brokerage | 0 | Discount brokers charge 0 on delivery |
| Slippage | 0.05% (large) to 0.50% (nano) per side | **Assumption**, there is no official figure. Change it in Settings. |

All of these can be changed in **Settings & data**.

## How the numbers are calculated

- **XIRR (headline)**: each buy is money paid on its date (including slippage and charges), each sale and dividend is money received, and the final cash flow is the market value of the shares still held (shares x latest official close), dated today. No daily price history is needed.
- **Fund XIRR**: every "add money" is paid in on its date, every "withdraw" comes back, and the final cash flow is today's total value (shares at the latest close plus idle cash). This shows the effect of keeping cash uninvested.
- Both use the yearly rate that makes the cash flows balance (365-day year, same as Excel). The *Money in / out & XIRR* page lists every cash flow and proves the sum is zero at that rate. Checked against Microsoft's own XIRR example (37.34%) and your example (₹1,00,000 start, ₹20,000 added on day 172, ₹2,00,000 after one year gives 73.26%).
- **Benchmark XIRR**: the same cash flows on the same dates put into the benchmark index at its official close. It needs the index value on those dates, so it shows "–" for any date before the stored index history.
- **Chart**: where daily closing prices are not stored (for example a stock bought before the tracker started), the stock's value is drawn as a straight line between your trade price and the latest official close. The chart says so when this estimate is used.
- **Time-weighted return** (the chart line): like a mutual fund NAV, so adding or withdrawing money does not move the line; only performance does.
- **Profit on sales and holding period**: first in, first out (as used for Indian tax). Buy cost includes slippage and charges.
- **Cash check**: a buy is refused if the portfolio does not have enough cash on that date, and a sale is refused if you do not hold enough shares.

## One-time setup (about 5 minutes, no downloads)

1. **Turn on the website**: in this repository on GitHub, open **Settings → Pages**. Under "Build and deployment" choose **Deploy from a branch**, branch **main**, folder **/ (root)**, and click **Save**. After a minute your site is at `https://virag1610.github.io/portfolio-tracker-2/`. Bookmark it.
2. **Create an access key** so the website can save your entries:
   - Go to **github.com → your photo → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   - Name: `Folio Lab`. Expiration: your choice (you will need a new key when it expires).
   - Repository access: **Only select repositories → portfolio-tracker-2**.
   - Permissions → Repository permissions: **Contents: Read and write** and **Actions: Read and write**.
   - Click **Generate token** and copy it.
3. **Open your site → Settings & data**, paste the key, click **Save & test connection**. The key is kept only in that browser (do it once per device).
4. **Portfolios → Create a portfolio** with a name, benchmark, starting money and start date.
5. **Add entry → Buy**: paste the NSE symbol (e.g. `RELIANCE`) or BSE code (e.g. `500325`), quantity and rate. The first time you add a stock, the site asks GitHub to fetch its prices right away (1 to 3 minutes).

## Good to know

- This repository is **public**, so anyone who knows the address can see your entries (they are virtual trades). For a private setup, make the repository private and open `index.html` straight from your computer instead of GitHub Pages; the key lets it read and write the private repository.
- Use **Settings & data → Download backup file** now and then.
- If GitHub ever shows "scheduled workflow disabled" (it can happen after 60 days with no activity), open the **Actions** tab and click **Enable workflow**.
- The website works on phone too.

## Files

| File | Purpose |
|---|---|
| `index.html` | The whole website |
| `data/portfolio.json` | Your portfolios and entries (written by the website) |
| `data/prices.json` | Official closing prices of held stocks and indices (written by the daily job) |
| `data/symbols.json` | List of all NSE and BSE codes and names, for checking codes as you type |
| `scripts/update_prices.py` | The daily price download (runs on GitHub, you never run it) |
| `.github/workflows/update-prices.yml` | The schedule for the daily job |
