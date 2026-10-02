"""在既有信号/止盈规则上逐账户重放资金、保护退出和开仓时间。"""
import numpy as np
from numba import njit

import backtest_engine as B


@njit(cache=True, nogil=True)
def replay_account(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                   tx_l, tx_s, tp_l, tp_s, mode, p1, p2,
                   close, high, low, size, year_index, year_count,
                   cooldown, slippage, initial, min_qty, enforce_minimum, max_qty,
                   funding_cum, funding_rate, protect_ratio, maintenance_rate, cross,
                   partial=False, fraction=0.0, next_l=None, next_s=None,
                   close_confirmed=False, plan_cache=None,
                   min_entry_gap=0, entry_fee_rate=0.0, exit_fee_rate=0.0):
    # sizes为空时旧内核只求一笔交易的信号退出，不进行账户资金计算。
    # 复用它避免维护第二套MACD、固定止盈、移动止盈及分批退出判据。
    no_sizes = np.empty(0, dtype=np.float64)
    equity = peak = 1.0
    mdd = 0.0
    protections = forced = stopped = 0
    nt = wins = longs = orders = holding = 0
    waiting = 0
    first_capacity_entry = -1
    total = total2 = profit = loss = 0.0
    yearly = np.zeros(year_count, dtype=np.float64)
    p = 0
    last_run = -1
    for k in range(len(cand)):
        i = int(cand[k])
        if i < p or run_id[i] == last_run:
            continue
        entry = close[i]
        quantity = min(initial * equity * size / entry, max_qty)
        if equity <= 0.0 or (enforce_minimum and quantity < min_qty):
            stopped = 1
            break
        effective = quantity * entry / (initial * equity)
        side = int(cdir[k])
        cached = plan_cache is not None and plan_cache[k, 0] >= 0
        if cached:
            duration = int(plan_cache[k, 0])
            raw_return = plan_cache[k, 1]
        elif partial:
            planned = B.simulate_partial_sizes(
                cand[k:k+1], cdir[k:k+1], run_id, sx_l, sx_s, sp_l, sp_s,
                tx_l, tx_s, tp_l, tp_s, fraction, mode, next_l, next_s, p1, p2,
                close, high, low, no_sizes, year_index, year_count)
        else:
            planned = B.simulate_all_sizes(
                cand[k:k+1], cdir[k:k+1], run_id, sx_l, sx_s, sp_l, sp_s,
                tx_l, tx_s, tp_l, tp_s, mode, p1, p2,
                close, high, low, no_sizes, year_index, year_count)
        if not cached:
            duration = int(planned[4]) if planned[0] else 0
            raw_return = planned[5] if planned[0] else 0.0
            if plan_cache is not None:
                plan_cache[k, 0] = duration
                plan_cache[k, 1] = raw_return
        exit_i = i + duration
        if exit_i <= i:
            continue
        if first_capacity_entry < 0 and quantity >= max_qty:
            first_capacity_entry = i
        price_return = raw_return
        stop_i = int(sx_l[i] if side > 0 else sx_s[i])
        first_i = int(tx_l[i] if side > 0 else tx_s[i])
        first_price = tp_l[i] if side > 0 else tp_s[i]
        did_partial = partial and first_i < stop_i
        first_return = (first_price / entry - 1.0) * side if did_partial else 0.0
        if close_confirmed:
            # 信号仍按保守OHLC路径触发；只改变成交价，不声称Maker必成交。
            last_return = (close[exit_i] / entry - 1.0) * side
            if did_partial:
                first_return = (close[first_i] / entry - 1.0) * side
                price_return = fraction * first_return + (1.0 - fraction) * last_return
            else:
                price_return = last_return
        took_profit = did_partial if partial else exit_i < stop_i
        trade_orders = 3 if did_partial else 2
        # 与原模型同样使用固定MMR的全仓近似；真实阶梯维持保证金需另接历史规则。
        liq_threshold = max(0.0, 1.0 - effective * maintenance_rate)
        liquidation_first = cross and liq_threshold <= protect_ratio
        threshold = liq_threshold if liquidation_first else protect_ratio
        risk_exit = False
        # 费率按成交名义金额而非保证金计收；开仓费在开仓时立即影响权益。
        # 滑点仍沿用旧模型的收益扣减，不能混入费率再重复扣一次。
        opening_equity = max(0.0, equity * (1.0 - effective * entry_fee_rate))
        mdd = max(mdd, 1.0 - opening_equity / peak)
        for j in range(i + 1, exit_i + 1):
            remaining = 1.0
            realized = 0.0
            paid_fees = entry_fee_rate
            if did_partial and j > first_i:
                remaining = 1.0 - fraction
                realized = fraction * first_return
                paid_fees += fraction * (1.0 + side * first_return) * exit_fee_rate
            # 开仓在i的收盘后；i自身的极值不能成为持仓风险。
            adverse_price = low[j] if side > 0 else high[j]
            marked_return = realized + remaining * (adverse_price / entry - 1.0) * side
            adverse_loss = effective * (paid_fees - marked_return)
            if adverse_loss >= threshold:
                exit_i = j
                took_profit = False  # 保护退出属于止损，不再等待止盈冷却。
                trade_orders = 3 if did_partial and j > first_i else 2
                # 已扣费用会提前耗尽风险预算；用剩余预算还原保护价，
                # 再对实际已平部分及本次剩余平仓金额计费。
                price_return = -threshold / effective + paid_fees
                if liquidation_first:
                    forced += 1
                else:
                    protections += 1
                risk_exit = True
                break
            # 只用已经发生的收盘权益抬高峰值；本根极值不假定先高后低。
            worst_equity = max(0.0, equity * (1.0 + effective * (marked_return - paid_fees)))
            mdd = max(mdd, 1.0 - worst_equity / peak)
            if j < exit_i:
                if did_partial and j == first_i:
                    remaining = 1.0 - fraction
                    realized = fraction * first_return
                    paid_fees += fraction * (1.0 + side * first_return) * exit_fee_rate
                at_close = realized + remaining * (close[j] / entry - 1.0) * side
                close_equity = max(0.0, equity * (1.0 + effective * (at_close - paid_fees)))
                mdd = max(mdd, 1.0 - close_equity / peak)
                peak = max(peak, close_equity)
        # 加权价格收益可精确还原总平仓名义金额/开仓名义金额，
        # 包括多空、分批以及保护提前退出，不按订单数重复收整仓手续费。
        if risk_exit and liquidation_first:
            net_return = -1.0 / effective  # 全仓归零已包含费用，不能再扣成负资金。
        else:
            exit_notional_ratio = 1.0 + side * price_return
            net_return = price_return - slippage - entry_fee_rate - exit_fee_rate * exit_notional_ratio
        if funding_rate != 0.0:
            net_return -= funding_rate * (funding_cum[exit_i] - funding_cum[i])
        nt += 1
        orders += trade_orders
        holding += exit_i - i
        total += net_return
        total2 += net_return * net_return
        if net_return > 0:
            wins += 1
            profit += net_return
        elif net_return < 0:
            loss -= net_return
        if side > 0:
            longs += 1
        yearly[year_index[exit_i]] += net_return
        if risk_exit and liquidation_first:
            equity = 0.0
        else:
            equity = min(1e300, max(0.0, equity * (1.0 + effective * net_return)))
        mdd = max(mdd, 1.0 - equity / peak)
        peak = max(peak, equity)
        last_run = run_id[i]
        if took_profit:
            waiting += min(cooldown, len(close) - 1 - exit_i)
        # 旧止盈冷却按“完整跳过N根”保留；新间隔按平仓至开仓的分钟差。
        # 例如平仓在10:00，间隔5分钟允许10:05，两个约束取max而非相加。
        p = max(exit_i + (cooldown if took_profit else 0) + 1,
                exit_i + min_entry_gap)
        if equity <= 0.0:
            stopped = 1
            break
    if enforce_minimum and len(close):
        if min(initial * equity * size / close[-1], max_qty) < min_qty:
            stopped = 1
    return (nt, wins, longs, orders, holding, total, total2, profit, loss, yearly,
            np.array([equity]), np.array([mdd]), np.array([protections]),
            np.array([nt]), np.array([stopped]), np.array([forced]), waiting, first_capacity_entry)


def _merge_accounts(results, year_count):
    first = results[0]
    account_stats = np.array([
        [*r[:9], *r[9]] for r in results
    ], dtype=np.float64).reshape(len(results), 9 + year_count)
    return (*first[:10], *(np.concatenate([r[i] for r in results])
                          for i in range(10, 16)), account_stats,
            np.array([r[16] for r in results], dtype=np.float64),
            np.array([r[17] for r in results], dtype=np.int64))


def simulate_all_sizes(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                       tx_l, tx_s, tp_l, tp_s, mode, p1, p2,
                       close, high, low, sizes, year_index, year_count,
                       cooldown=0, slippage=0.0, initial=100.0, min_qty=0.0,
                       enforce_minimum=False, max_qty=100.0,
                       funding_cum=B.FUNDING_CUM, funding_rate=0.0,
                       protect_ratio=0.85, maintenance_rate=0.005, cross=True,
                       close_confirmed=False, cache_plans=True,
                       min_entry_gap=0, entry_fee_rate=0.0, exit_fee_rate=0.0):
    # 同一入场的信号退出与杠杆无关；只共享计划，资金/强平/重开仍逐账户独立。
    plans = np.full((len(cand), 2), -1.0) if cache_plans and len(sizes) > 1 else None
    results = [replay_account(
        cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s, tx_l, tx_s, tp_l, tp_s,
        mode, p1, p2, close, high, low, size, year_index, year_count,
        cooldown, slippage, initial, min_qty, enforce_minimum, max_qty,
        funding_cum, funding_rate, protect_ratio, maintenance_rate, cross,
        False, 0.0, tx_l, tx_s, close_confirmed, plans,
        min_entry_gap, entry_fee_rate, exit_fee_rate)
        for size in sizes]
    return _merge_accounts(results, year_count)


def simulate_partial_sizes(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                           tx_l, tx_s, tp_l, tp_s, fraction, mode, next_l, next_s, p1, p2,
                           close, high, low, sizes, year_index, year_count,
                           cooldown=0, slippage=0.0, initial=100.0, min_qty=0.0,
                           enforce_minimum=False, max_qty=100.0,
                           funding_cum=B.FUNDING_CUM, funding_rate=0.0,
                           protect_ratio=0.85, maintenance_rate=0.005, cross=True,
                           close_confirmed=False, cache_plans=True,
                           min_entry_gap=0, entry_fee_rate=0.0, exit_fee_rate=0.0):
    plans = np.full((len(cand), 2), -1.0) if cache_plans and len(sizes) > 1 else None
    results = [replay_account(
        cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s, tx_l, tx_s, tp_l, tp_s,
        mode, p1, p2, close, high, low, size, year_index, year_count,
        cooldown, slippage, initial, min_qty, enforce_minimum, max_qty,
        funding_cum, funding_rate, protect_ratio, maintenance_rate, cross,
        True, fraction, next_l, next_s, close_confirmed, plans,
        min_entry_gap, entry_fee_rate, exit_fee_rate) for size in sizes]
    return _merge_accounts(results, year_count)
