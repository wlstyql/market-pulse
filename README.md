# MARKET PULSE — Tistory public version

## Deploy (GitHub Pages)

1. Create a **public GitHub repository** named `market-pulse`.
2. Upload **all files**, including `.github/workflows/collect.yml` (hidden folder).
3. In repository **Settings → Actions → General**, set Workflow permissions to **Read and write permissions**, if the organization/repository requires it.
4. In **Settings → Pages**, select **Deploy from a branch**, branch `main`, folder `/docs`. Save.
5. In **Actions → Collect market data → Run workflow**, run once. Check the Actions log and `docs/market_data.json` for numeric data and any `errors`.
6. Open `https://YOUR-USERNAME.github.io/market-pulse/` and verify it shows collected numbers with timestamps. A scheduled run is attempted hourly at minute 17 UTC, but GitHub may delay or skip scheduled runs.
7. In Tistory post HTML editor, add:

```html
<iframe src="https://YOUR-USERNAME.github.io/market-pulse/" style="width:100%;height:1250px;border:0;border-radius:16px" loading="lazy" title="MARKET PULSE"></iframe>
```

## Important limitations

- No official public feed is guaranteed for all metrics. Providers can block GitHub IPs, apply anti-bot checks or change HTML structure.
- `market_data.json` starts **empty**; it never pretends old example values are current.
- The site marks collections over **3 hours old** as stale. Each indicator can still have its own older original observation date, especially quarterly Buffett indicators.
- KOSPI fear/greed is an independently calculated proxy, **not** the official CNN formula.
- Collecting 1-hourly does **not** mean underlying statistics refresh hourly.
- Historical price change for DDR/NAND requires a history archive; default cached history on stateless GitHub runners isn't persisted. Missing fields remain missing.
- This is a heuristic score and not a statistically validated 2–3 month return predictor.
- Tistory may sanitize iframe markup; verify published article on both desktop and mobile.
