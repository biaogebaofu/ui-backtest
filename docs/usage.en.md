# Illustrated Usage Guide

[中文](usage.zh-CN.md) | [Project overview](../README.en.md)

This guide follows the Chinese UI of the public v1.72 copy. All eight operational screenshots show actual Windows / Tkinter windows with **2,400 synthetic one-minute bars**. The demonstration compares 2 take-profit plans × 1x / 2x, producing 4 independent account results. It illustrates controls and file exports, not actual market performance, trading, or profitability. See the [README](../README.en.md) for installation and authorization terms.

The workflow is **select and inspect data → choose a small configuration → check costs and account constraints → run → inspect files → separately export rankings or candidates**. Complete this small workflow before expanding the search.

`demo-data.csv` and `demo-results` are illustration names and are not shipped with the repository. Use your own prepared market data and a new result directory.

## 1. Data sources and capabilities

![Primary K-lines and auxiliary data capability checks](images/01-data.png)

Open `1. 数据源与能力` (Data sources and capabilities). Click `浏览…` beside `1m主K线` to select the primary file. It must meet the [input schema](../README.en.md#input-data), including at least 500 valid, consecutive minute-aligned bars. Add microstructure, funding, or Open Interest files if available, or select a ZIP. Explicit files take precedence over matching sources inside a ZIP. OHLCV alone is sufficient to begin research with basic rules.

Click `检测数据与策略覆盖` (Inspect data and strategy coverage). Check the instrument, bar count, UTC dates, time gaps, and the capability table: `能力 / 状态 / 覆盖率 / 用途` means capability / status / coverage / use. Unsupported selections are automatically unchecked and greyed out. `按当前数据修剪策略` trims the current selection again using the inspection result. Buttons such as `第三批全选当前可用` select supported rules; availability does not establish their effectiveness.

Funding and OI are aligned backward without future records. Incomplete auxiliary coverage can leave fewer rules usable on particular timeframes; a column name alone does not establish valid coverage. Reinspect after changing source paths.

The UTC start field `开始日期/时间（UTC）` is inclusive; `结束日期/时间（不含）` is exclusive. Leave both blank to use the entire file. A selected interval still needs enough consecutive data.

## 2. Entry: establish one small baseline

![Multi-timeframe entry rules and entry constraints](images/02-entry.png)

Open `2. 组合选择` → `1. 开仓条件`. For a first run, `仅1分钟基础，高周期关闭` narrows the selection to basic one-minute entry with higher timeframes disabled. Then check the actual selections: one MACD calculation basis, one entry-frequency rule, one position filter, and few entry conditions. Every timeframe must retain a valid choice; use its disabled option when excluding a higher timeframe.

Multiple conditions on one timeframe are tested separately by default; selections across timeframes form cross combinations. Enabling `指标组合` explicitly enumerates strategies requiring two or more same-timeframe conditions together. Selecting multiple entry-frequency rules also tests separate scenarios rather than stacking every frequency restriction onto one account.

Review the live combination count at the top. The `全选当前策略（组合很大）` button can expand many dimensions at once. Stars and `已测最优` labels mark an existing baseline; they do not identify the best rule for your dataset.

The fifth batch is limited to the retained 39 definitions, with their native timeframe and data requirements. There is no switch to restore the other 81. These methods generate independent directional and one-time events; explicitly selected older rules still act as filters. Ordinary MACD entry timing does not define every fifth-batch method.

## 3. Stops: filtering does not clear selections

![Signal stops, fixed stops, and holding-time choices](images/03-stop.png)

In `2. 止损（含持仓时间）`, choose signal stops, fixed-percentage stops, and holding-time limits. Filter the list by round, category, timeframe, or keyword. **Filtering changes the visible list, not selections outside that list.** Use `只看已选` (Show selected) to review choices, and `筛选结果全部取消` or individual clicks to remove unwanted rules.

Begin with one signal-stop configuration. If fixed stops are unnecessary, use `只保留OFF` in that section. For custom percentages, check the weekday / weekend fields and click `加入所选` (Add to selection). Multiple percentage choices create separate combinations.

Time stops exit at the close when the limit is reached, whether profitable or not; `0` disables them. Account protection and liquidation settings on the run page have separate roles and must also be checked.

## 4. Take-profit: review the plan's units

![Take-profit plans, filters, and fixed-percentage overlay](images/04-take-profit.png)

Open `3. 止盈（含持仓时间）`. Locate a plan by category, timeframe, indicator, keyword, or ID, then click its selection state in the list. The screenshot compares plans #1 / #2; you can keep just one for a first run and confirm using `只看已选`. Changing filters does not clear other selected plans.

The fixed-percentage overlay `叠加固定比例止盈` runs alongside the selected dynamic plan; whichever triggers first exits. Use that section's `只保留OFF` when no overlay is needed. Weekday / weekend input fields use percentages. Parameters in the plan table vary by plan and cannot all be interpreted as percentages: for example, the moving-retracement filter explains that coefficient `0.01` means 1% of floating profit. Read the plan's explanation.

Time take-profit differs from a time stop: it requires a positive floating return at the time limit. Review `4. 止盈后等待` too. `0分钟（不等待）` is the no-extra-wait baseline. This wait applies only after take-profit exits; the run page's `所有平仓后最小等待` applies after all exits.

## 5. Position / leverage: independent accounts

![Position fractions and leverage choices](images/05-position.png)

Open `5. 仓位 / 杠杆`. Use `清空` (Clear), then select only `1.0仓` and `2x` for a small comparison. Each take-profit configuration replays both accounts; the two illustrated plans produce 4 account results in total. They do not share balances, and adding their returns does not describe a shared-capital portfolio.

Nominal leverage determines intended quantity, subject to initial funds and the minimum / maximum ETH order sizes on the run page. It does not ensure every order achieves that leverage or multiply profit. Candidate leverage settings only select corresponding results already computed; they do not rerun or scale untested position sizes.

The summary distinguishes account-result counts from full CSV row counts. A raw CSV strategy row can contain semicolon-separated arrays for several position sizes. Interpret them with `所选仓位顺序` (Selected position order); CSV rows are not necessarily individual accounts.

## 6. Costs and account constraints

![Execution offsets, fees, and run constraints](images/06-cost.png)

In `3. 运行与进度`, select at least one mode under `成本模式与平仓后等待`: `成交偏移` (Execution offset) or `手续费` (Fees). Selecting both creates two independent scenarios; they are not charged together on a trade. Unused inputs may remain saved, but are not that result's active costs.

| Location | Unit and example |
| --- | --- |
| Opening / closing offset `%` fields and fee fields | `0.01` means **0.01%**; entering `0.0001` would mean a smaller percentage |
| Saved configuration / CLI JSON rate and offset fields | Fraction `0.0001` means **0.01%** |
| `返佣比例%` (Rebate percentage) | `10` means a 10% rebate; also check BNB discount and displayed net rates |
| Candidate `压力场景往返偏移%（示例）` | A round-trip stress assumption, not an automatic fee applied separately to each side |

The default illustrative offset is 0.01% per side. The source's `USDC普通用户 · 双边吃单` fee preset is 0.04% per side. These are input presets, not current exchange quotes or your account's actual costs. Candidate stress values of 0.05% / 0.10% are also illustrative and need calibration.

Check the exit-fill convention, initial funds, ETH quantity limits, protective exits, liquidation settings, and S3 gate. The fixed funding assumption uses `%/8小时` (percent per eight hours); it does not automatically charge every record in a historical funding file. Distinguish the minimum wait after every exit from the take-profit-only wait described earlier.

## 7. Run, pause, and stop safely

![Run setup and controls in the waiting-to-start state](images/07-run.png)

The screenshot is in `等待开始` (Waiting to start), so pause and safe-stop controls are disabled. Actual status appears in this page's log and progress area after starting.

Before starting, check `当前选择范围` (Current scope), the CSV size estimate, CPU concurrency, and output directory. Keep `新任务自动创建独立文件夹，防止覆盖旧结果` enabled to create separate task folders. Use `下次建新任务` when starting a new task. The current per-account path uses CPU even in automatic device mode.

In editor mode, click `开始 / 断点继续` (Start / continue checkpoint). Follow the log through data validation, feature / signal preparation, scanning, and saving. Initial Numba compilation can take time without increasing completed rows; check the log before attempting another launch.

`暂停` is a request that takes effect after the current small task. The button changes to `继续`, which resumes execution. `安全停止` preserves completed results. A normal single task can continue from its last complete take-profit plan under unchanged code, environment, data, and parameters. Wait for the log and controls to show that processing has stopped before closing the window.

`删除原断点并从头重新计算` deletes that task's old checkpoint and starts over; it is not ordinary resume. Code or data changes require a new task. The exact fingerprint list runs independent tasks sequentially; stopping prevents later entries from starting, and this batch mode currently has no checkpoint resume.

During exports, `安全停止` stops the corresponding CSV scan rather than resuming the original backtest. **There is no backtest ranking table on this page.** After completion, use `打开结果目录` (Open result directory) to inspect files.

## 8. Files, rankings, and candidates

![Candidate scheme, gates, and export controls](images/08-candidates.png)

| Output | What it answers |
| --- | --- |
| `全部回测结果.csv` | All combinations actually completed, including losses and zero-trade results. A partial task's file does not prove the whole plan completed |
| Best / worst ranking Excel files | Account results ranked by the selected metric. Best rankings apply configured gates; the worst 1000 use all completed account results with valid ranking values, without best-ranking gates |
| Candidate Excel / CSV exports | Already-computed results passing the selected candidate scheme and gates. Exporting changes neither trading parameters nor untested leverage results |

In `4. 候选筛选与导出`, choose `本轮收益回撤优选` (Current-run return / drawdown selection) or `原严格成本预筛` (Legacy strict cost screening). The first filters eligible accounts, retains those above a proportion of the highest eligible ending funds, then sorts by drawdown. The proportion refers to ending funds, not net profit. Compare all tested leverage values or a specified value, and check trade samples, drawdown, PF, protective exits, and export limits. Grey controls may apply only to the legacy scheme.

`启用候选筛选` and `回测完成后自动生成候选Excel` control candidate processing after completion. You can also click `从已有CSV生成候选` and select the complete raw CSV. The candidate page's `选择已有结果目录并生成候选` button opens a **CSV file picker**, despite its label. To change ranking limits or metrics, use `从CSV重新导出排行榜`; no trade replay is needed.

Open the exported Excel files with a spreadsheet application. Candidates have research and observation tiers. This version lacks the detailed evidence needed for strict advanced validation and keeps `实盘候选` (Live-trading candidates) empty. The short 2,400-bar demonstration has no candidates under the default minimum of 200 complete trades and other gates; that is a valid export result. Ranking colors and returns do not establish out-of-sample validation.

### Read an actual demonstration export

![Synthetic backtest reading preview generated from an actual Excel export](images/09-export.png)

This preview was generated by reading the demonstration's actual Excel file. **It is not an Excel screenshot or an embedded application ranking.** It expands 4 account rows, while the raw CSV has 2 take-profit combination rows containing two position-size arrays each. Trade counts of 64 or 128 fall below the default 200-trade gate; all four accounts lose money. A best-ranking export can therefore exist without a profitable strategy or eligible research candidate.

Read the take-profit plan, nominal leverage, completed trades, ending funds, return, and drawdown together, then check that row's active costs and fingerprint. Original configuration records establish provenance; colors merely help compare values within the same table.

## Fingerprints need their original source

At the top of the combination page, enter `组合指纹（表中策略指纹）` and optionally `来源目录`, then click `添加／同步`. The program verifies original ranking / candidate records, run configuration, data, and dates before applying the settings. It does not decode every parameter from the fingerprint text.

Retain the **original task directory, configuration JSON, raw results, and matching data** together. A fingerprint or Excel file alone is insufficient. Synchronizing compatible fingerprints into the editor can add cross combinations. To rerun only the original entries, select `指纹精确列表（逐条独立）`. Applying a configuration does not start execution; you still start it manually, with a new output directory.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing `tkinter` / `_tkinter` | Run `python -m tkinter` in the same environment; install Tk through your Python installer or distribution. Linux also needs a desktop or virtual display |
| Boxes or missing Chinese glyphs | Install a system font supporting Chinese, then reopen the UI. The English documentation does not switch the current UI to English |
| No usable strategies or many grey options | Reinspect fields, consecutive minutes, the selected interval, and timeframe coverage. Begin with basic OHLCV rules; missing funding / OI / raw trades must not be fabricated |
| Completed run has no candidates | Check the log, full CSV, and screening statistics for trades and failed gates. Research candidates may be empty; live candidates are always empty in this version |
| GPU is present but CPU is used | The current per-account replay path uses CPU; installing CuPy does not accelerate it |
| An old task will not resume | Changes in version, data, Python / numeric dependencies, or frozen settings can trigger identity rejection. Create a new result directory; do not remove identity records to bypass checks |

Windows / Python 3.13.2 has been verified locally. See [GitHub Actions](https://github.com/biaogebaofu/ui-backtest/actions) for three-platform automated test status. macOS / Linux have not been tested on actual machines. For headless four-stage research, see [Research campaigns](research-campaign.md).
