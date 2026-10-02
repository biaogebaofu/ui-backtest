# 分层研究任务 / Staged Research Campaigns

`campaign_runner.py` 调用本地离线 `backtest_worker.py`，按规则族进行 `coarse → bucket → refine → validation` 四阶段研究。它按预设时间和资源预算运行，不穷举所有参数笛卡尔积，不连接交易所或下单。安装、行情格式与版权条款见 [中文说明](../README.md) / [English README](../README.en.md)；以下运行命令同样只面向版权所有者及另行获得运行授权的人。

研究 CLI 以 **Windows / macOS / Linux** 为目标，本身无需 Tk 窗口。macOS / Linux 尚未完成实机运行验证，跨平台 CI 待跑。

## 配置与命令

在项目目录准备 UTF-8 编码的 `settings.json`。下面是格式示例，须把路径与全部时间改成自己实际数据覆盖范围，并核对资金、手续费及数量上限：

```json
{
  "csv": "data/market.csv",
  "start": "2024-01-01T00:00:00Z",
  "end": "2024-03-01T00:00:00Z",
  "validation_start": "2024-03-01T00:00:00Z",
  "validation_end": "2024-04-01T00:00:00Z",
  "purge_gap_hours": 24,
  "wall_hours": 72,
  "max_jobs": 2000,
  "top_families": 8,
  "seed_selection": {
    "手续费": {
      "开仓费率": 0.0002,
      "平仓费率": 0.0002,
      "BNB抵扣": false,
      "返佣比例": 0
    },
    "资金约束": {
      "初始资金USDC": 20000,
      "最大开仓数量ETH": 100
    }
  }
}
```

`csv`、可选的 `micro_csv`、`funding`、`oi` 相对路径以设置文件所在目录为基准。研究 runner 不接受隐式 ZIP `bundle`，须先解包并明确填写各文件路径。费用是小数比例，例中 `0.0002` 为 0.02%；研究基准要求开、平手续费均大于零，不能用 100% 返佣归零。费用和成交偏移是分别回测的成本情景。

```sh
python campaign_runner.py init --settings settings.json --root campaign/run01
python campaign_runner.py run --root campaign/run01
python campaign_runner.py status --root campaign/run01
```

`init` 只接受尚不存在的新任务目录，冻结代码、依赖、数据与设置的身份；`run` 启动或继续该任务，`status` 输出摘要。源文件、Python / 数值依赖版本、数据或冻结设置发生变化时，应新建任务，不能混用旧断点。按 Ctrl+C 请求停止，可保留已完成结果和可用断点；再次 `run` 仍受原任务预算约束，不会重新获得 72 小时。

## 时间、预算与输出

时间按 UTC 处理，区间为左闭右开。须满足 `start < end <= validation_start < validation_end`。训练区间分成两个窗口，并在窗口间及最后训练窗口末尾设置 `purge_gap_hours` 隔离间隔；所选训练区间必须足够长。冻结复核区间不参与候选筛选，但历史可能已被看过，不能宣称真正未见过的未来样本外验证。

runner 每次只运行一个 CPU worker，使用一个工作线程。默认总壁钟预算 72 小时、最多登记 2000 个任务、本任务目录预算 12 GiB，至少保留 15 GiB 磁盘空闲；可用内存低于 900 MiB 不启动，运行中低于 700 MiB 请求停止并等候。资源检查是软件侧保护，不是操作系统级硬限制，实际部署应根据电脑或服务器调整预算。`max_jobs` 可设置 1–10000，初始计划超过该值会拒绝初始化。

任务目录中保存：

- `settings.json`、`identity.json`、`catalog.json`：冻结配置、源数据与环境身份、规则定义。
- `state.json`、`summary.json`、`coverage.json`：任务状态、摘要和实际覆盖情况。
- `analysis.json`、`研究结果汇总.md`：训练候选、冻结时间段复核、未通过原因与限制。
- `jobs/`：逐任务参数、日志和结果；完成的 `全部回测结果.csv` 经 gzip 回读 SHA256 校验后保存为 `全部回测结果.csv.gz`，才移除对应原 CSV。亏损和零交易记录也保留。
- `cache/`、`tmp/`、`runner.log`：本任务缓存、临时文件与调度日志。

缺数据、失败、预算延期和未完成任务均单独登记，不能计为已完成覆盖。完成阶段或通过描述性复核门槛不证明找到盈利策略。第五轮沿用源码策略，仅保留的 39 项参与研究目录；不可用与未运行的状态不伪装成零信号完成。

## English

The runner invokes the offline worker for four stages: `coarse`, `bucket`, `refine`, and `validation`. It performs budgeted family coverage rather than an exhaustive Cartesian search. The target platforms are **Windows / macOS / Linux**. macOS / Linux have not been tested on actual machines, and cross-platform CI is pending. No graphical Tk session is needed for the research CLI.

Create UTF-8 `settings.json` using the shared example above. Replace the path and every date with the actual dataset range and check the fees, starting funds, and ETH quantity cap. Relative `csv`, `micro_csv`, `funding`, and `oi` paths resolve beside the settings file. Unpack ZIP sources first: the runner rejects implicit `bundle` inputs. Fee values are decimal fractions, so `0.0002` means 0.02%. Positive opening and closing fees are required, without a 100% rebate. Fees and execution offsets are separate scenarios.

Run the three commands above from the project directory. `init` requires a new, nonexistent task directory. `run` starts or resumes it, and `status` prints its summary. Code, runtime dependencies, data, and settings are frozen; changes require a new task. Ctrl+C requests a stop while preserving committed results and available checkpoints. Resuming retains the original wall-time deadline.

UTC windows are start-inclusive and end-exclusive. They must satisfy `start < end <= validation_start < validation_end`. Two training windows and the final training boundary include the configured purge gap. Validation is excluded from candidate selection, but historical observations may already have been seen; this is not evidence of an unseen future sample.

One single-thread CPU worker runs at a time. Defaults are 72 wall-clock hours, 2000 registered jobs, a 12 GiB task-directory budget, and at least 15 GiB free disk. Starting requires 900 MiB available memory; below 700 MiB during execution triggers a stop request and waiting. These are software checks, not operating-system hard limits. `max_jobs` accepts 1–10000, and initialization rejects an initial plan exceeding it.

The task directory contains frozen settings and identity, the rule catalog, state and coverage summaries, `analysis.json`, a Chinese research report, per-job records, caches, and logs. Completed result CSVs are removed only after gzip round-trip SHA256 verification. Losses and zero-trade rows are retained. Missing-data, failed, deferred, and unfinished jobs are reported separately. The retained 39 fifth-round definitions remain governed by the source policy. Coverage and descriptive validation thresholds do not establish future profitability.
