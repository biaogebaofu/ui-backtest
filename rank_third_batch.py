"""第三批1m开仓规则统一出口压力测试。

目的不是拟合止损/止盈，而是在完全相同退出条件下比较“入场本身”。
三套对称固定SL/TP（0.3/0.4/0.6%）分别跑，1x、无S3闸门、无等待、
往返成交偏移0.01%，LIVE_01同一1m MACD方向段最多一笔。

重要：固定止损/止盈若直到样本末尾都未触发，统一在最后一根收盘价结算；
不能把“永不触发”的N下标传进Numba循环，否则会越界读取并伪造极端回撤。
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

from extended_rules import ENTRY_RULES, THIRD_ROUND_CODES, PLACEBO_CODES
import backtest_engine as B

ROUNDTRIP = 0.00005 + 0.00005
REGIMES = (0.003, 0.004, 0.006)
SIZES = np.array([1.0], dtype=np.float64)


def bounded_fixed_stop(rate: float):
    """固定止损转成安全的样本内退出；未触发则样本末收盘结算。"""
    x_l, x_s, p_l, p_s = B.fixed_stop_data(rate, rate)
    x_l = np.asarray(x_l).copy(); x_s = np.asarray(x_s).copy()
    p_l = np.asarray(p_l).copy(); p_s = np.asarray(p_s).copy()
    miss_l = x_l >= B.N; miss_s = x_s >= B.N
    x_l[miss_l] = B.N - 1; x_s[miss_s] = B.N - 1
    p_l[miss_l] = B.CLOSE[-1]; p_s[miss_s] = B.CLOSE[-1]
    return x_l, x_s, p_l, p_s


def run_one(field, cases, cand, cdir, run_id, rate):
    sx_l, sx_s, sp_l, sp_s = bounded_fixed_stop(rate)
    tx_l, tx_s, tp_l, tp_s = B.fixed_rate_data(rate)
    result = B.simulate_all_sizes(
        cand, cdir, run_id,
        sx_l, sx_s, sp_l, sp_s,
        tx_l, tx_s, tp_l, tp_s,
        0, 0.0, 0.0,
        B.CLOSE, B.HIGH, B.LOW, SIZES, B.YEAR_INDEX, len(B.YEAR_VALUES),
        0, ROUNDTRIP,
        100.0, 0.0, False, 100.0,
        funding_rate=0.0, protect_ratio=0.85, maintenance_rate=0.005, cross_liquidation=True,
    )
    m = B.metrics_dict(result)
    return {
        'field': field, 'code': cases[-1], 'rate': rate,
        'trades': m['交易次数'], 'win_rate': m['胜率'], 'long_share': m['多单占比'],
        'avg_hold_min': m['平均持仓分钟'], 'avg_trade_ret': m['平均单笔收益率'],
        'gross_return': m['毛收益合计'], 'profit_factor': m['盈亏比'], 't_value': m['t值'],
        'final_return': float(m['期末资金倍数'][0] - 1.0), 'max_drawdown': float(m['最大回撤'][0]),
        'protect_count': int(m['爆仓保护次数'][0]), 'forced_count': int(m['全仓强平次数'][0]),
        'candidate_signals': int(len(cand)),
    }


def pct_rank(values, higher=True):
    s = pd.Series(values, dtype=float)
    if not higher:
        s = -s
    return s.rank(method='average', pct=True).to_numpy(float)


def main(out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    detailed = []
    cases_all = tuple((0, 0, 0, 0, c) for c in THIRD_ROUND_CODES)
    for field in ('hist', 'dif'):
        S, EX, run_id, entries, _ = B.build_field(
            field, selected_cases=cases_all, selected_stop_codes=('OFF',),
            s3_gap=0.0, direction='BOTH', session='ALL', materialize_stops=False,
            entry_mode='LIVE_01')
        for cases, cand, cdir in entries:
            for rate in REGIMES:
                detailed.append(run_one(field, cases, cand, cdir, run_id, rate))

    detail = pd.DataFrame(detailed)
    agg = detail.groupby(['field', 'code'], as_index=False).agg(
        平均期末收益率=('final_return', 'mean'),
        最差期末收益率=('final_return', 'min'),
        最好期末收益率=('final_return', 'max'),
        三口径盈利数=('final_return', lambda x: int((np.asarray(x) > 0).sum())),
        平均最大回撤=('max_drawdown', 'mean'),
        最差最大回撤=('max_drawdown', 'max'),
        平均盈亏比=('profit_factor', 'mean'),
        平均t值=('t_value', 'mean'),
        最少交易次数=('trades', 'min'),
        平均交易次数=('trades', 'mean'),
        平均胜率=('win_rate', 'mean'),
        平均持仓分钟=('avg_hold_min', 'mean'),
        原始候选信号=('candidate_signals', 'mean'),
        爆仓保护次数=('protect_count', 'sum'),
        全仓强平次数=('forced_count', 'sum'),
    )

    # 稳健分：优先最大化“最差退出口径”的净收益，其次才看平均收益、回撤与统计质量。
    # 这是入场筛选，不允许某一个特别漂亮的退出档掩盖另外两个档的严重亏损。
    p_worst = pct_rank(agg['最差期末收益率'], True)
    p_avg = pct_rank(agg['平均期末收益率'], True)
    p_dd = pct_rank(agg['平均最大回撤'], False)
    p_pf = pct_rank(agg['平均盈亏比'], True)
    p_t = pct_rank(agg['平均t值'], True)
    raw_score = 0.60*p_worst + 0.20*p_avg + 0.10*p_dd + 0.05*p_pf + 0.05*p_t
    sample_penalty = np.minimum(1.0, np.sqrt(np.maximum(agg['最少交易次数'].to_numpy(float), 0.0)/20.0))
    agg['稳健得分'] = raw_score * sample_penalty * 100.0
    agg['MACD口径'] = agg['field'].map({'hist':'MACD柱', 'dif':'DIF线'})
    agg['策略名称'] = agg['code'].map(lambda c: ENTRY_RULES[int(c)][0])
    agg['类别'] = agg['code'].map(lambda c: '安慰剂/噪声对照' if int(c) in PLACEBO_CODES else ('订单流/微结构' if int(c) in set(range(53,61))|set(range(96,112)) else '价格/指标'))
    agg['是否安慰剂'] = agg['code'].map(lambda c: '是' if int(c) in PLACEBO_CODES else '否')

    # 先稳健得分，再看最差/平均收益。稳健得分本身已经把最差收益权重放到60%。
    agg = agg.sort_values(['稳健得分','最差期末收益率','平均期末收益率','平均t值'], ascending=[False,False,False,False]).reset_index(drop=True)
    agg.insert(0, '排名', np.arange(1, len(agg)+1))
    for c in ['平均期末收益率','最差期末收益率','最好期末收益率','平均最大回撤','最差最大回撤','平均胜率']:
        agg[c+'（%）'] = agg[c]*100.0
    cols = ['排名','code','策略名称','MACD口径','类别','是否安慰剂','稳健得分','三口径盈利数',
            '平均期末收益率（%）','最差期末收益率（%）','最好期末收益率（%）',
            '平均最大回撤（%）','最差最大回撤（%）','平均盈亏比','平均t值','最少交易次数','平均交易次数',
            '平均胜率（%）','平均持仓分钟','原始候选信号','爆仓保护次数','全仓强平次数']
    ranking = agg[cols]
    ranking.to_csv(out/'第三批全部策略_统一口径排名.csv', index=False, encoding='utf-8-sig')

    # 用户要“最好前10”：主表排除故意安慰剂；另保留全体前10作为数据挖掘警报对照。
    serious = ranking[ranking['是否安慰剂']=='否'].copy().reset_index(drop=True)
    serious['排名'] = np.arange(1, len(serious)+1)
    serious.head(10).to_csv(out/'第三批Top10_严肃策略.csv', index=False, encoding='utf-8-sig')
    ranking.head(10).to_csv(out/'第三批Top10_包含安慰剂.csv', index=False, encoding='utf-8-sig')
    detail.to_csv(out/'第三批逐退出口径明细.csv', index=False, encoding='utf-8-sig')

    meta = {
      '数据': 'ETHUSDT Binance Futures；由6,365,188条aggTrades聚合为10,080根连续1m K线',
      '时间范围UTC': '2026-04-24 00:00:00 至 2026-05-01 00:00:00（不含）',
      '测试对象': f'第三批代码18—127 × MACD柱/DIF线，共{len(THIRD_ROUND_CODES)*2}个入场候选',
      '第三批规则数': len(THIRD_ROUND_CODES),
      '其中安慰剂规则数': len(PLACEBO_CODES),
      '统一退出': ['固定止损=固定止盈=0.3%', '固定止损=固定止盈=0.4%', '固定止损=固定止盈=0.6%'],
      '样本末处理': '固定止损/止盈直到样本末仍未触发时，按最后一根1m收盘价结算，避免越界/虚假极端回撤',
      '仓位': '1x名义倍数；不启用最小下单量停机',
      '成交成本': f'往返固定偏移{ROUNDTRIP:.6%}（开0.005%+平0.005%）',
      '入场节奏': 'LIVE_01；4h/1h/15m/5m关闭；1m第三批条件；同一1m MACD方向段最多一次',
      'S3闸门': '关闭（0%），避免把结构闸门混进入场规则比较',
      '等待': '0分钟',
      '评分': '60%最差退出口径净收益百分位 +20%平均净收益 +10%低回撤 +5%盈亏比 +5%t值；最少交易<20笔按sqrt(n/20)降权',
      'Top10口径': '主Top10排除故意安慰剂；同时导出包含安慰剂的全体Top10，用于观察样本内噪声强度',
      '重要限制': '仅7天样本。Top10只能视为候选发现，不是实盘结论；至少需要更长时间、不同市场阶段和样本外/滚动前推复核。',
    }
    (out/'测试口径与限制.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    print('=== 严肃策略 Top 20 ===')
    print(serious.head(20).to_string(index=False))
    print('\n=== 全体 Top 10（含安慰剂） ===')
    print(ranking.head(10).to_string(index=False))
    print('\n'+json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    import sys
    main(sys.argv[1] if len(sys.argv)>1 else 'third_batch_rank')
