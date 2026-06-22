# Asset Allocation Dashboard
### NDR · BlackRock Target Allocation ETF · BCA Research

A self-updating research dashboard comparing tactical asset allocation views across three major firms. Hosted free on GitHub Pages, refreshed automatically on the 1st of every month via GitHub Actions.

---

## What it does

- Pulls allocation signals from Ned Davis Research, BlackRock, and BCA Research
- Displays Overweight / Underweight / Neutral views for 15 asset classes
- Shows broad Equity / Debt / Cash splits per firm
- Tracks month-over-month changes automatically
- Maintains a 12-month rolling history chart

---

## File structure

```
dashboard/
├── index.html                    ← Dashboard UI
├── style.css                     ← All styles
├── script.js                     ← All frontend logic (reads allocations.json)
├── requirements.txt              ← Python dependencies
│
├── data/
│   ├── allocations.json          ← AUTO-GENERATED each month (don't edit manually)
│   └── allocations_fallback.json ← Seed data used before first live run
│
├── python/
│   └── update_data.py            ← Master orchestrator script
│
├── scrapers/
│   ├── ndr_scraper.py            ← NDR API + web session fallback
│   ├── blackrock_scraper.py      ← Aladdin API + iShares CSV + BII web fallback
│   └── bca_scraper.py            ← BCA API + web session + PDF parser fallback
│
└── .github/
    └── workflows/
        └── monthly_update.yml    ← GitHub Actions cron scheduler
```

---

## Setup — Step by step

### Step 1 — Get the code onto your computer

1. Install [Git](https://git-scm.com/downloads) and [VS Code](https://code.visualstudio.com/) (or [Cursor](https://cursor.com))
2. Open VS Code, press `Ctrl+Shift+P`, type **Git: Clone**
3. Paste your GitHub repo URL and choose a local folder

### Step 2 — View the dashboard locally

1. In VS Code, install the **Live Server** extension (search in Extensions panel)
2. Right-click `index.html` → **Open with Live Server**
3. The dashboard opens in your browser showing fallback data

### Step 3 — Install Python dependencies

Open the terminal in VS Code (`Ctrl+\``):

```bash
pip install -r requirements.txt
```

### Step 4 — Run the scraper locally (demo mode first)

```bash
# Test with no API credentials — uses fallback data
python python/update_data.py --demo

# Run with live credentials (set env vars first — see Step 5)
python python/update_data.py
```

This writes `data/allocations.json`. Refresh your browser — the dashboard updates.

### Step 5 — Set your API credentials (local)

Create a `.env` file in the project root (this file is gitignored — never commit it):

```bash
# .env  — never commit this file
NDR_USER=your_ndr_username
NDR_PASS=your_ndr_password_or_api_key
NDR_API_URL=https://api.ndr.com/v2

BL_USER=your_blackrock_username
BL_PASS=your_blackrock_password_or_token
BL_API_URL=https://api.blackrock.com/aladdin/v1

BCA_USER=your_bca_subscriber_id
BCA_PASS=your_bca_api_key
BCA_API_URL=https://api.bcaresearch.com/v1
```

Load them before running:

```bash
# Mac/Linux
export $(cat .env | xargs) && python python/update_data.py

# Windows PowerShell
Get-Content .env | ForEach-Object { $k,$v = $_ -split '=',2; [System.Environment]::SetEnvironmentVariable($k,$v) }
python python/update_data.py
```

### Step 6 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial dashboard"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 7 — Enable GitHub Pages

1. Go to your repo on GitHub
2. Click **Settings** → **Pages**
3. Under **Source**, select **Deploy from a branch**
4. Branch: **main**, Folder: **/ (root)**
5. Click **Save**

Your dashboard is live at: `https://YOUR_USERNAME.github.io/YOUR_REPO/`

### Step 8 — Add credentials as GitHub Secrets

These are encrypted — GitHub injects them into the Action at runtime, they never appear in logs.

1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add each:

| Name | Value |
|---|---|
| `NDR_USER` | Your NDR username |
| `NDR_PASS` | Your NDR password or API key |
| `BL_USER` | Your BlackRock/iShares username |
| `BL_PASS` | Your BlackRock password or API token |
| `BCA_USER` | Your BCA subscriber ID |
| `BCA_PASS` | Your BCA API key or password |

### Step 9 — Test the GitHub Action manually

1. Go to your repo → **Actions** tab
2. Click **Monthly Allocation Update** in the left sidebar
3. Click **Run workflow** → choose demo mode or live
4. Watch the logs — check that data commits back to main

After that, it runs automatically on the **1st of every month at 9 AM UTC**.

---

## Adapting the scrapers to real API responses

Each scraper has extensive comments showing exactly what to update. The key files are:

**`scrapers/ndr_scraper.py`**
- Update `NDR_ASSET_MAP` keys to match NDR's actual asset class names in their API/portal
- Update `NDR_SIGNAL_MAP` to match their stance labels
- Update the login form field names in `_fetch_via_web()`

**`scrapers/blackrock_scraper.py`**
- The iShares CSV path (Method 2) works **without any credentials** — it reads public fund holdings
- Update `BL_ASSET_MAP` to match how BII labels their asset classes
- The weight-to-signal conversion in `_weight_to_signal()` can be tuned

**`scrapers/bca_scraper.py`**
- Update login form field names in `_fetch_via_web()`
- BCA publishes PDF reports — `_parse_pdf()` uses PyMuPDF to extract text
- Update `BCA_ASSET_MAP` label strings to match BCA's exact terminology

**Best approach in Cursor:**
1. Log in to the provider portal manually in your browser
2. Open DevTools → Network tab → trigger a page load
3. Find the API call that returns allocation data
4. Paste the response JSON into Cursor and say: *"Map this response to my ASSET_MAP schema"*

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Dashboard shows "could not load data/allocations.json" | Run `python python/update_data.py --demo` first |
| API auth fails | Check credentials in `.env`, verify API endpoint URL with provider |
| Web scrape fails | Provider may have changed their HTML — inspect the page in DevTools and update CSS selectors |
| GitHub Action fails | Check the Actions log; add `--demo` flag in the workflow_dispatch input to test without credentials |
| Changes not showing on GitHub Pages | Wait 2-3 minutes after a push; GitHub Pages has a propagation delay |

---

## License

MIT — use freely, attribution appreciated.
