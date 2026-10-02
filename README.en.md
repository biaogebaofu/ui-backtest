# UI Backtest · Desktop Strategy Backtesting Tool

English | [中文](README.md)

A local, offline Python / Tkinter tool for **ETH perpetual strategy research**, based on **v1.72**. Configure strategy combinations in the Chinese UI, replay accounts independently by position size, and export complete results, rankings, and research candidates as CSV / Excel. A separate command-line runner supports staged research by rule family.

**The application UI is currently Chinese; the documentation is bilingual.** Market data, personal settings, historical results, and exchange credentials are not included. The program does not place exchange orders.

## Features and intended use

| Feature | Purpose |
| --- | --- |
| K-line, microstructure, funding, and OI capability checks | Verify fields and time continuity before choosing supported rules |
| Multi-timeframe entry, stop, take-profit, waiting, and position choices | Establish a small baseline and compare changes one factor at a time |
| Independent cost scenarios and per-position account replay | Examine costs, order-size caps, and account risk under explicit assumptions |
| Complete CSV, best / worst rankings, and candidate exports | Retain all results while selecting account results for further research |
| Result fingerprints and staged research CLI | Recover verifiable configurations or perform traceable, budgeted family research |

Useful for checking data, comparing strategy parameters, and organizing offline experiments. Account parameters use ETH / USDC conventions. Historical results do not guarantee profit or suitability for live trading. **The UI has no embedded backtest ranking table; inspect exported CSV / Excel files.**

## A visual walkthrough

These are actual Windows screenshots of the public copy using **2,400 fictional one-minute bars**. The demonstration compares 2 take-profit plans × 1x / 2x, producing 4 independent account results. Screenshots and results illustrate the workflow, not actual trading or profitability. See the full eight-stage [English guide](docs/usage.en.md) / [中文操作指南](docs/usage.zh-CN.md).

`demo-data.csv` and `demo-results` are illustration names and are not shipped with the repository. Select your own prepared market data for your first backtest.

### 1. Check what the data supports

Select the primary K-line file and optional auxiliary sources in `1. 数据源与能力`, then click `检测数据与策略覆盖`. Review dates, continuity, and field coverage first. Rules without required data are unchecked and greyed out.

![Data sources and capability checks](docs/images/01-data.png)

### 2. Establish a small baseline

In `2. 组合选择`, configure entry, stops, take-profit, waiting, and position sizes. Start with few selections and review the live combination count. Multiple choices usually create independent combinations, so checkbox counts alone do not describe the workload.

![Entry rules and combination selection](docs/images/02-entry.png)

### 3. Check costs and account constraints, then run

`3. 运行与进度` collects the data, output directory, CPU concurrency, costs, funds, and risk settings. The screenshot is in `等待开始` (Waiting to start), showing setup and control buttons. Click `开始 / 断点继续` and follow the log. `暂停` and `安全停止` requests take effect at safe calculation boundaries.

![Run setup and controls in the waiting-to-start state](docs/images/07-run.png)

### 4. Generate candidates from saved results

After completion, click `打开结果目录` and inspect the files in a spreadsheet application. Set candidate gates and use `从已有CSV生成候选` to select `全部回测结果.csv`. Exporting does not rerun the backtest. A short demonstration may have no research candidates; this version keeps the live-trading candidate sheet empty.

![Candidate filtering and export settings](docs/images/08-candidates.png)

### Reading exported results

This reading preview was generated from the demonstration's actual Excel export. **It is neither an Excel screenshot nor an embedded UI table.** The raw CSV has 2 take-profit combination rows, each containing 1x / 2x account arrays; the ranking expands them into 4 account rows. All four accounts lose money here. “Best” describes ordering under the selected metric, not profitability or eligibility under candidate gates.

![Reading preview of synthetic backtest exports](docs/images/09-export.png)

## Environment and installation

The target desktop platforms are Windows, macOS, and Linux, using **Python 3.13**. Windows / Python 3.13.2 has been verified locally. See [GitHub Actions](https://github.com/biaogebaofu/ui-backtest/actions) for the current status of automated tests on all three platforms. macOS / Linux have not been tested on actual machines.

You need Python with Tkinter / Tcl/Tk, a graphical desktop session, and a font that displays Chinese. Check that `python -m tkinter` opens a window. Tkinter is an optional Python standard-library module; if missing, install it through your Python installer or distribution. See the [official Python Tkinter documentation](https://docs.python.org/3/library/tkinter.html).

Download and extract the repository, then open a terminal in its directory. Use a separate environment and the existing [requirements.txt](requirements.txt).

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tkinter
.\.venv\Scripts\python.exe ui.py
```

macOS / Linux, after checking that `python3` is your intended version and includes Tkinter:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m tkinter
python ui.py
```

In an active, correctly configured Python environment, run `python ui.py` directly. Initial dependency installation requires downloads; ordinary backtests read local files. Initial Numba compilation and feature preparation can take time.

You can also use the [Windows PowerShell launcher](scripts/start.ps1) (`.\scripts\start.ps1`) or [macOS / Linux launcher](scripts/start.sh) (`sh scripts/start.sh`). Both prefer the project's `.venv` environment.

Personal settings are stored outside the checkout; the main file is `用户设置.json`:

| Platform | Settings directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\ETHBacktest`, falling back to `%USERPROFILE%\AppData\Local\ETHBacktest` if unset or not absolute |
| macOS | `~/Library/Application Support/ETHBacktest` |
| Linux | `$XDG_CONFIG_HOME/ETHBacktest`, falling back to `~/.config/ETHBacktest` if unset or not absolute |

The default results directory is `~/ETHBacktest/results`. Set `UI_BACKTEST_HOME` to use its `settings/` and `results/` subdirectories instead. You can still select a different output directory in the UI.

## First backtest

1. Select your own one-minute K-line file and any auxiliary sources on the data-source page, then run the data check.
2. Choose UTC dates within the actual data range. The start is inclusive and the end is exclusive. Select a new output directory.
3. Begin with a small set of entry / exit combinations and position sizes. Check costs, funds, and risk settings before starting. Rules lacking required fields are marked unavailable; OHLCV alone cannot reproduce actual order flow or order books.
4. Inspect and export results. To restore a configuration from a fingerprint, retain the original task directory, configuration records, and corresponding data. Excel alone or a fingerprint alone is insufficient for reliable restoration.

Use new tasks when code, data, or parameters change. Large combination spaces can consume substantial time and disk space; review the displayed counts, disk estimate, and thread settings before running.

For button-by-button instructions, cost units, pause and stop behavior, export differences, and troubleshooting, see the [complete usage guide](docs/usage.en.md).

## Input data

The primary source accepts **CSV or Parquet (`.parquet` / `.pq`)**, with one row per one-minute bar. CSV requires a header with these columns:

| Column | Meaning |
| --- | --- |
| `openTime` | UTC bar opening time; Unix milliseconds are recommended, aligned to minute boundaries |
| `open`, `high`, `low`, `close` | Positive OHLC prices with valid high / low relationships |
| `volume` | Nonnegative base-asset traded volume |

This fictional two-row example illustrates the schema; it is too short to run a backtest:

```csv
openTime,open,high,low,close,volume
1704067200000,2000,2002,1999,2001,10
1704067260000,2001,2003,2000,2002,12
```

Validation requires at least **500 valid, consecutive one-minute bars**. Identical rows can be deduplicated; conflicting rows for a minute or missing minutes are rejected. The parser also accepts consistent modern Unix seconds / microseconds / nanoseconds, or parseable UTC / ISO timestamp strings. Do not mix timestamp units or numeric values with strings within one column. `open_time` and `open_time_ms` are aliases for `openTime`.

Auxiliary sources also accept CSV / Parquet:

| Source | Columns |
| --- | --- |
| Optional K-line fields | `quote_volume`, `trades`, `taker_buy_base`, `taker_buy_quote`, etc. Supported aliases include `quoteVolume`, `numberOfTrades`, and `takerBuyBaseVolume` |
| Minute-level microstructure | `openTime`, plus available fields among `quote_volume`, `agg_trades`, `trades`, `taker_buy_base`, `taker_sell_base`, `taker_buy_quote`, `delta_base`, `taker_buy_ratio`, `avg_trade_size` |
| Raw aggregate trades | Required: `timestamp,price,quantity,is_buyer_maker`. The flag accepts `true/false` or `1/0`. The complete short-field group `p,q,T,m` is also recognized; IDs may use `agg_trade_id`, `first_trade_id`, `last_trade_id` |
| Settled funding rates | `funding_time` (or `timestamp`) and `funding_rate`; aliases include `fundingTime,fundingRate` |
| Open interest | `timestamp` and at least one of `open_interest,open_interest_value`; aliases include `sumOpenInterest,sumOpenInterestValue` |

Sources must describe the same instrument and use consistent volume units. A `symbol` column helps identify mismatches. Funding and OI use backward matching at bar close without future records. Matching tolerances are 24 hours for funding and one hour for OI; expired records are not treated as current values.

The UI also accepts ZIP bundles. Primary filenames must contain `klines_1m`, `kline_1m`, or `1m_kline`; auxiliary names use `agg_trades` / `aggtrades`, `funding_rates` / `funding_rate`, or `open_interest` / `openinterest`. If a type has multiple matching files, merge its partitions first or select an explicit file.

## Research runner and tests

Headless research uses coarse screening, parameter buckets, refinement, and a frozen chronological check. It covers rule families within a budget, **not the full Cartesian product**. Both the desktop entry point and research CLI target all three platforms; macOS / Linux verification is pending as described above. See [Research campaigns / 研究任务说明](docs/research-campaign.md).

Run the shipped regression tests in an environment with Tk and graphical display:

```sh
python run_self_tests.py
```

Tests use temporary synthetic data and do not require private data or real-market downloads. The full suite includes GUI tests; headless Linux requires a virtual display. Passing tests does not establish financial validity.

## GPU and result limitations

NVIDIA / CUDA is not required. `requirements-gpu.txt` preserves optional experimental CuPy CUDA 12 dependencies, but the current per-account replay path runs on **CPU**. Detecting a GPU or installing CuPy does not accelerate this path. These CUDA dependencies do not support macOS Metal / Apple GPUs.

Fees and execution offsets can be compared as independent cost scenarios; review the actual parameters used. The default example execution offset is 0.01% on opening and closing. Fees depend on the selected preset or input: the source's `USDC普通用户 · 双边吃单` preset uses 0.04% per side, which is not a claim about your account's actual rate. Candidate execution-offset stress scenarios use 0.05% / 0.10%. Calibrate assumptions to your market and execution method. Historical returns, rankings, and research candidates do not guarantee future profit. Results depend on data quality, fill assumptions, account constraints, and the scope of selection. A chronological check of historical data is not necessarily an unseen future sample and does not establish suitability for live trading.

## Publication notes and copyright

This public copy was prepared from v1.72. Private execution calibrations and derived research cost thresholds were replaced with public demonstration values, so defaults and a few research thresholds differ from the personal version and should not be claimed to reproduce its historical results. Use a new result directory; version-identity checks reject mixed old checkpoints. Original data, historical CSVs, and fingerprint records were not changed.

The source is public for viewing with copyright reserved. **This is not an MIT-licensed or other open-source project.** The installation and execution instructions are intended for the copyright owner and separately authorized users. See [LICENSE](LICENSE); GitHub's terms still permit platform viewing, forking, and related service functionality.

See [CONTRIBUTING.md](CONTRIBUTING.md) for feedback and [SECURITY.md](SECURITY.md) for security reports. Third-party dependencies retain their own licenses. Copyright and public-viewing terms are in [LICENSE](LICENSE); platform permissions are described in the [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#5-license-grant-to-other-users).
