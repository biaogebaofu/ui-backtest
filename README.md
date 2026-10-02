# UI Backtest · 桌面策略回测工具

[English](README.en.md) | 中文

基于 Python / Tkinter 的本地离线回测界面，发布基线为 **v1.72**。通过组合选择配置入场、退出、仓位、成本及账户约束，查看排行、导出 CSV / Excel，并按结果指纹查找原配置。另有按规则族分阶段运行的无人值守研究命令行工具。

**界面目前为中文，文档提供中英两版。** 仓库不包含行情数据、个人设置、历史回测结果或交易所凭据；本程序不连接交易所下单。

这份公开副本由 v1.72 整理，将私人真实成交校准及其派生研究成本阈值泛化为公开演示值。因此默认参数和少量研究门槛与个人原版不同，不能声称默认运行会复现原有结果。请使用新结果目录；程序版本身份核验会拒绝混用旧断点。整理过程未修改原数据、历史 CSV 或指纹记录。

源码公开供查看，版权保留，**不是 MIT 或其他开源许可证项目**。安装和运行说明面向版权所有者及另行获得运行授权的人。具体权限见 [LICENSE](LICENSE)；公开仓库仍适用 GitHub 服务条款规定的平台查看、fork 等权限。

## 环境与安装

目标桌面系统为 Windows、macOS 和 Linux，使用 **Python 3.13**。Windows / Python 3.13.2 已完成本机验证；macOS / Linux 的 CI 尚待运行，也尚未完成实机验证。

需要带 Tkinter / Tcl/Tk 的 Python、可显示窗口的桌面会话，以及支持中文显示的字体。先用 `python -m tkinter` 检查是否能打开窗口。Tkinter 属于 Python 的可选标准库模块，缺少时请通过 Python 安装程序或所用发行版补装，参见 [Python 官方 Tkinter 文档](https://docs.python.org/3/library/tkinter.html)。

下载并解压仓库后，在项目目录打开终端。建议使用独立环境，安装现有 [requirements.txt](requirements.txt)。

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tkinter
.\.venv\Scripts\python.exe ui.py
```

macOS / Linux（先确认 `python3` 是目标版本且提供 Tkinter）：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m tkinter
python ui.py
```

已进入正确 Python 环境时，直接运行 `python ui.py` 即可。首次安装需要下载依赖；普通回测使用本地文件。Numba 首次编译和特征预计算可能需要等待。

也可使用启动脚本：[Windows PowerShell](scripts/start.ps1)（`.\scripts\start.ps1`）或 [macOS / Linux](scripts/start.sh)（`sh scripts/start.sh`）；脚本优先使用项目内的 `.venv`。

个人设置保存于用户目录，主要设置文件为 `用户设置.json`：

| 系统 | 设置目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\ETHBacktest`；未设置或不是绝对路径时用 `%USERPROFILE%\AppData\Local\ETHBacktest` |
| macOS | `~/Library/Application Support/ETHBacktest` |
| Linux | `$XDG_CONFIG_HOME/ETHBacktest`；未设置或不是绝对路径时用 `~/.config/ETHBacktest` |

默认结果目录为 `~/ETHBacktest/results`。设置环境变量 `UI_BACKTEST_HOME` 可改用该目录下的 `settings/` 保存设置、`results/` 保存默认结果；仍可在界面另选结果目录。

## 第一次回测

1. 在数据源页面选择自己准备的 1 分钟 K 线文件，以及需要的辅助数据，再执行数据检查。
2. 选择实际数据覆盖范围内的 UTC 起止时间；开始时间包含，结束时间不包含。指定一个新的结果目录。
3. 从少量入场 / 退出组合和仓位开始，确认成本、资金与风险参数，再运行。缺字段的规则会被标为不可用；仅有 OHLCV 不能补出真实订单流或盘口。
4. 查看结果并导出。需要通过指纹恢复配置时，保留原任务目录、配置记录及对应数据；仅有 Excel 或指纹不能保证完整还原。

不要将不同代码版本、数据或参数的任务混入旧断点。组合规模可能很大，运行前留意界面的数量、磁盘估算和线程设置。

## 数据格式

主数据支持 **CSV、Parquet（`.parquet` / `.pq`）**，每行是一根 1 分钟 K 线。CSV 必须有表头，必需列如下：

| 列 | 含义 |
| --- | --- |
| `openTime` | UTC 开盘时间；推荐 Unix 毫秒时间戳，必须对齐整分钟 |
| `open`, `high`, `low`, `close` | 开、高、低、收价格，均须为正，且满足 OHLC 关系 |
| `volume` | 基础币成交量，须非负 |

以下仅演示格式，是虚构数据；两行不足以运行回测：

```csv
openTime,open,high,low,close,volume
1704067200000,2000,2002,1999,2001,10
1704067260000,2001,2003,2000,2002,12
```

检查要求至少 **500 根有效、连续的分钟 K 线**。完全重复的行可去重，同一分钟内容冲突或缺口会报错。解析器也接受同一列内单位一致的现代 Unix 秒 / 微秒 / 纳秒时间戳，或可解析的 UTC / ISO 时间文本；不要混用单位或数值与文本。`open_time`、`open_time_ms` 可作为 `openTime` 的别名。

辅助数据同样支持 CSV / Parquet：

| 数据 | 列格式 |
| --- | --- |
| K 线内可选字段 | `quote_volume`、`trades`、`taker_buy_base`、`taker_buy_quote` 等；例如 `quoteVolume`、`numberOfTrades`、`takerBuyBaseVolume` 是支持的别名 |
| 按分钟汇总的微结构 | `openTime`，以及 `quote_volume`、`agg_trades`、`trades`、`taker_buy_base`、`taker_sell_base`、`taker_buy_quote`、`delta_base`、`taker_buy_ratio`、`avg_trade_size` 中实际拥有的字段 |
| 原始聚合成交 aggTrades | 必需 `timestamp,price,quantity,is_buyer_maker`；布尔字段接受 `true/false` 或 `1/0`。完整短字段组 `p,q,T,m` 也可识别；可带 `agg_trade_id`、`first_trade_id`、`last_trade_id` |
| 已结算资金费率 | `funding_time`（或 `timestamp`）和 `funding_rate`；支持 `fundingTime,fundingRate` 别名 |
| 持仓量 OI | `timestamp` 和 `open_interest` / `open_interest_value` 中至少一列；支持 `sumOpenInterest,sumOpenInterestValue` 别名 |

各文件须来自同一品种、使用一致的成交量单位。可提供 `symbol` 字段协助核对。资金费率和 OI 按 K 线收盘时刻向后匹配，不使用未来记录；资金费率匹配容许 24 小时，OI 容许 1 小时，过期记录不视作当前有效数据。

UI 也可读取 ZIP 数据包：主文件名应含 `klines_1m`、`kline_1m` 或 `1m_kline`；辅助文件名分别含 `agg_trades` / `aggtrades`、`funding_rates` / `funding_rate`、`open_interest` / `openinterest`。每种类型存在多个文件时需先合并分片或显式选单文件，不能依赖自动任选一个。

## 研究命令行与测试

无人值守研究采用粗筛、参数桶、细化及冻结时间段复核，按预算覆盖规则族，**不是全部参数笛卡尔积穷举**。桌面入口和研究 CLI 均以三平台为目标，macOS / Linux 的验证状态同上。配置、命令和输出见 [研究任务说明 / Research campaigns](docs/research-campaign.md)。

在有 Tk 桌面显示的环境中运行随仓库附带的回归测试：

```sh
python run_self_tests.py
```

测试使用临时合成行情，不需要私人数据或下载真实行情。完整测试包括 GUI，Linux 无桌面环境需要虚拟显示。通过测试不等于回测模型已得到金融有效性证明。

## GPU 与结果限制

普通安装不需要 NVIDIA / CUDA。`requirements-gpu.txt` 保留 CuPy CUDA 12 的可选实验依赖，但当前逐账户回放路径使用 **CPU**；检测到 GPU 或安装 CuPy 不会让这一路径自动获得 GPU 加速。macOS 的 Metal / Apple GPU 不属于该 CUDA 依赖的支持范围。

手续费和成交偏移可以作为独立成本情景比较，应核对实际使用的参数。发布默认成本仅为示例：开、平手续费各 0.01%，成交偏移压力情景为 0.05% / 0.10%，需要按自己的市场与成交方式校准。历史收益、排行和研究候选均不保证未来收益；结果受行情质量、成交假设、资金约束和筛选范围影响。历史时间段复核也不代表真正未见过的未来样本，不应直接视作实盘推荐。

问题反馈见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。第三方依赖遵循各自许可证。本项目版权及公开查看条款见 [LICENSE](LICENSE)，GitHub 平台权限见 [GitHub 服务条款](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#5-license-grant-to-other-users)。
