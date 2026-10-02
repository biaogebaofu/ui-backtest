"""Offline Maker simulation with standing TP, one scale-in and partial break-even.

Signals are known at candle CLOSE. Orders submitted then are eligible only on later
price segments. A fill can place TP for the remaining path of its own candle; both
OHLC orders are explicit test cases. Strict trade-through uses a tick tolerance.
"""
import numpy as np
from numba import njit
from hedge_config import normalize_config

MINUTE = 60000
DAY = 86400000
ENGINE_VERSION = "hedge-v1-standing-tp"
STAT_NAMES = ["ending_equity", "net_profit", "max_drawdown", "trades", "wins",
              "win_rate", "profit_factor", "daily_trades", "fees", "time_exits",
              "tp_exits", "average_hold_minutes", "max_hold_minutes",
              "max_timeout_delay_minutes", "first_capacity_ms", "open_positions",
              "unrealized_pnl", "min_equity", "entries", "min_entry_gap_minutes",
              "capacity_entries", "final_cash", "add_entries", "breakeven_exits",
              "return_rate", "max_direction_eth"]
TRADE_NAMES = ["side", "entry_ms", "exit_ms", "first_quantity", "added_quantity",
               "average_entry_price", "final_exit_price", "gross_pnl", "fees",
               "net_pnl", "exit_kind", "hold_minutes", "timeout_delay_minutes"]
FILL_NAMES = ["side", "time_ms", "kind", "quantity", "price", "average_entry_price",
              "gross_pnl", "fee", "cash", "equity", "remaining_quantity"]
POSITION_NAMES = ["side", "quantity", "average_entry_price", "first_entry_ms",
                  "added", "breakeven_quantity", "timeout_pending", "first_entry_price"]
TRADE_WIDTH = len(TRADE_NAMES)
FILL_WIDTH = len(FILL_NAMES)
POSITION_WIDTH = len(POSITION_NAMES)
# Fill kind 0=first entry, 1=scale-in, 2=break-even reduction, 3=TP, 4=timeout, 5=direction switch.


@njit(cache=True)
def _equity(cash, qty, avg, price):
    return cash + qty[0]*(price-avg[0]) + qty[1]*(avg[1]-price)


@njit(cache=True)
def _observe(cash, qty, avg, price, peak, drawdown, minimum):
    value = _equity(cash, qty, avg, price)
    peak = max(peak, value)
    if peak > 0: drawdown = max(drawdown, (peak-value)/peak)
    return peak, drawdown, min(minimum, value)


@njit(cache=True)
def _round_price(price, sell, tick):
    return (np.ceil(price/tick-1e-9) if sell else np.floor(price/tick+1e-9))*tick


@njit(cache=True)
def _passive(price, sell, tick):
    return max(tick, _round_price(price+tick if sell else price-tick, sell, tick))


@njit(cache=True)
def _cross(start, finish, limit, sell, eps):
    if sell: return finish > start and start <= limit+eps and finish > limit+eps
    return finish < start and start >= limit-eps and finish < limit-eps


@njit(cache=True, nogil=True)
def _simulate(ts, o, h, l, c, tp, coins, signals, runs, hours, initial, max_qty,
              first_multiple, add_multiple, add_drop, gap_minutes, fee, path, detail, directions):
    n=len(ts); tick=.01; step=.001; eps=tick*1e-6
    timeout_ms=int(hours*3600000); gap_ms=gap_minutes*MINUTE
    qty=np.zeros(2); avg=np.zeros(2); first_price=np.zeros(2); first_qty=np.zeros(2)
    added_qty=np.zeros(2); started=np.zeros(2,np.int64); added=np.zeros(2,np.int64)
    trim_qty=np.zeros(2); timed_out=np.zeros(2,np.int64)
    direction_exit=np.zeros(2,np.int64); direction_exits=0
    calendar_on=directions[0]!=0
    cycle_gross=np.zeros(2); cycle_fees=np.zeros(2)
    # TP and BE are standing orders. Timeout is repriced once per minute after expiry.
    target=np.full(2,np.nan); be_target=np.full(2,np.nan); exit_limit=np.full(2,np.nan)
    opening=np.full(2,np.nan); opening_qty=np.zeros(2); opening_kind=np.zeros(2,np.int64)
    opening_run=np.full(2,-1,np.int64); pending_since=np.zeros(2,np.int64)
    used_run=-2; last_entry=-10**15
    cash=initial; peak=initial; drawdown=0.; minimum=initial; min_gap=1e30
    totals=np.zeros(15)
    # Every first/add consumes the same opening gap. Each cycle has at most two exits.
    capacity=n//gap_minutes+8
    trades=np.empty((capacity if detail else 0,TRADE_WIDTH))
    fills=np.empty((capacity*3 if detail else 0,FILL_WIDTH))
    trade_count=0; fill_count=0; first_capacity=-1; maximum_qty=0.
    daily=np.empty((n//1440+3,5)); day_count=0; points=np.empty(5)
    for i in range(n):
        end=ts[i]+MINUTE
        # Calendar uses bar-open time, before any orders can fill on this bar's path.
        if calendar_on:
            for side in range(2):
                if directions[i] != (1 if side==0 else -1):
                    opening_kind[side]=0; opening[side]=np.nan
                    if qty[side]>0:
                        direction_exit[side]=1; target[side]=np.nan; be_target[side]=np.nan
                        exit_limit[side]=_passive(c[i-1] if i else o[i],side==0,tick)
        points[0]=c[i-1] if i else o[i]; points[1]=o[i]
        points[2]=h[i] if path==0 else l[i]; points[3]=l[i] if path==0 else h[i]; points[4]=c[i]
        peak,drawdown,minimum=_observe(cash,qty,avg,points[0],peak,drawdown,minimum)
        for segment in range(1,5):
            start=points[segment-1]; finish=points[segment]; up=finish>start
            # Two sides, one opening fill (cancels other opening orders), BE then TP.
            for event in range(8):
                chosen=-1; kind=-1; level=1e100 if up else -1e100
                for side in range(2):
                    sell=side==0
                    if qty[side]>0:
                        for exit_kind in range(3):
                            value=(exit_limit[side] if exit_kind==2 else
                                   (be_target[side] if exit_kind==1 else target[side]))
                            if _cross(start,finish,value,sell,eps) and ((up and value<level) or (not up and value>level)):
                                chosen=side; kind=(5 if direction_exit[side] else 4) if exit_kind==2 else (2 if exit_kind==1 else 3); level=value
                    transition=calendar_on and ((direction_exit[0] and qty[0]>0) or (direction_exit[1] and qty[1]>0))
                    if not transition and opening_kind[side]>0 and _cross(start,finish,opening[side],side==1,eps):
                        value=opening[side]
                        if (up and value<level) or (not up and value>level):
                            chosen=side; kind=opening_kind[side]-1; level=value
                if chosen<0: break
                side=chosen; peak,drawdown,minimum=_observe(cash,qty,avg,level,peak,drawdown,minimum)
                gross=0.; amount=0.
                if kind<2:
                    amount=opening_qty[side]; charge=amount*level*fee
                    if kind==0:
                        qty[side]=amount; avg[side]=level; first_price[side]=level
                        first_qty[side]=amount; added_qty[side]=0.; started[side]=end
                        added[side]=0; trim_qty[side]=0.; cycle_gross[side]=0.; cycle_fees[side]=charge
                        used_run=opening_run[side] if opening_run[side]>=0 else used_run
                        totals[0]+=1
                    else:
                        avg[side]=(qty[side]*avg[side]+amount*level)/(qty[side]+amount)
                        qty[side]+=amount; added_qty[side]=amount; trim_qty[side]=amount
                        added[side]=1; cycle_fees[side]+=charge; totals[1]+=1
                        be=avg[side]*(1+fee)/(1-fee) if side==0 else avg[side]*(1-fee)/(1+fee)
                        be_target[side]=_round_price(be,side==0,tick)
                    cash-=charge; totals[2]+=charge
                    min_gap=min(min_gap,(end-last_entry)/MINUTE); last_entry=end
                    for other in range(2): opening_kind[other]=0; opening[other]=np.nan
                    target[side]=_round_price(avg[side]*(1+tp[i] if side==0 else 1-tp[i]),side==0,tick)
                    exit_limit[side]=np.nan; timed_out[side]=0
                    maximum_qty=max(maximum_qty,qty[side])
                    if qty[side]>=max_qty-step*.1:
                        totals[3]+=1
                        if first_capacity<0: first_capacity=end
                else:
                    amount=trim_qty[side] if kind==2 else qty[side]
                    gross=amount*(level-avg[side])*(1 if side==0 else -1)
                    charge=amount*level*fee; cash+=gross-charge
                    cycle_gross[side]+=gross; cycle_fees[side]+=charge; totals[2]+=charge
                    qty[side]-=amount
                    if kind==2:
                        trim_qty[side]=0.; be_target[side]=np.nan; totals[4]+=1
                    else:
                        net=cycle_gross[side]-cycle_fees[side]
                        hold=(end-started[side])/MINUTE
                        delay=max(0.,(end-started[side]-timeout_ms)/MINUTE) if kind==4 else 0.
                        if detail:
                            trades[trade_count]=np.array([side,started[side],end,first_qty[side],added_qty[side],
                                avg[side],level,cycle_gross[side],cycle_fees[side],net,kind,hold,delay])
                        trade_count+=1; totals[5]+=net>0; totals[6]+=max(net,0.); totals[7]+=max(-net,0.)
                        totals[8]+=kind==4; totals[9]+=kind==3; totals[10]+=hold
                        direction_exits+=kind==5
                        totals[11]=max(totals[11],hold); totals[12]=max(totals[12],delay)
                        qty[side]=0.; trim_qty[side]=0.; added[side]=0; timed_out[side]=0
                        direction_exit[side]=0
                        target[side]=np.nan; be_target[side]=np.nan; exit_limit[side]=np.nan
                        opening_kind[side]=0; opening[side]=np.nan
                peak,drawdown,minimum=_observe(cash,qty,avg,level,peak,drawdown,minimum)
                if detail:
                    fills[fill_count]=np.array([side,end,kind,amount,level,avg[side],gross,charge,cash,
                                               _equity(cash,qty,avg,level),qty[side]])
                fill_count+=1; start=level
            peak,drawdown,minimum=_observe(cash,qty,avg,finish,peak,drawdown,minimum)
        next_tp=tp[i+1] if i+1<n else tp[i]
        for side in range(2):
            # New direction takes effect at this exact close; cancel stale entry orders.
            if calendar_on and directions[i+1] != (1 if side==0 else -1):
                opening_kind[side]=0; opening[side]=np.nan
                if qty[side]>0: direction_exit[side]=1
            if qty[side]<=0: continue
            if direction_exit[side]:
                target[side]=np.nan; be_target[side]=np.nan
                exit_limit[side]=_passive(c[i],side==0,tick)
                opening_kind[side]=0; opening[side]=np.nan
            elif end>=started[side]+timeout_ms:
                timed_out[side]=1; target[side]=np.nan; be_target[side]=np.nan
                exit_limit[side]=_passive(c[i],side==0,tick)
                opening_kind[side]=0; opening[side]=np.nan
            elif next_tp!=tp[i]:
                value=avg[side]*(1+next_tp if side==0 else 1-next_tp)
                # A changed TP already behind the market is reposted passively, never taker.
                passive=_passive(c[i],side==0,tick)
                value=max(value,passive) if side==0 else min(value,passive)
                target[side]=_round_price(value,side==0,tick)
        transition=calendar_on and ((direction_exit[0] and qty[0]>0) or (direction_exit[1] and qty[1]>0))
        if i+1<n and not transition and end-last_entry>=gap_ms and _equity(cash,qty,avg,c[i])>0:
            pending=-1
            for side in range(2):
                if calendar_on and directions[i+1] != (1 if side==0 else -1):
                    continue
                if opening_kind[side]==1:
                    if end-pending_since[side]>=2*MINUTE: opening_kind[side]=0; opening[side]=np.nan
                    else: pending=side
                if qty[side]>0 and not added[side] and not timed_out[side] and add_multiple>0:
                    value=first_price[side]*(1-add_drop if side==0 else 1+add_drop)
                    passive=_passive(c[i],side==1,tick)
                    value=min(value,passive) if side==0 else max(value,passive)
                    value=_round_price(value,side==1,tick)
                    amount=np.floor(min(_equity(cash,qty,avg,c[i])*add_multiple/value,
                                        max(0.,max_qty-qty[side]))/step+1e-9)*step
                    if amount>=.001 and amount*value>=20:
                        opening[side]=value; opening_qty[side]=amount; opening_kind[side]=2
                    else:
                        opening_kind[side]=0; opening[side]=np.nan
            if pending<0:
                permitted=runs[i]<0 or runs[i]!=used_run
                free0=qty[0]==0 and signals[i,0] and permitted and (not calendar_on or directions[i+1]>=0)
                free1=qty[1]==0 and signals[i,1] and permitted and (not calendar_on or directions[i+1]<=0)
                if free0 or free1:
                    pending=int(coins[i]) if free0 and free1 else (0 if free0 else 1)
                    pending_since[pending]=end; opening_run[pending]=runs[i]
            if pending>=0:
                value=_passive(c[i],pending==1,tick)
                amount=np.floor(min(_equity(cash,qty,avg,c[i])*first_multiple/value,max_qty)/step+1e-9)*step
                if amount>=.001 and amount*value>=20:
                    opening[pending]=value; opening_qty[pending]=amount; opening_kind[pending]=1
                else: opening_kind[pending]=0; opening[pending]=np.nan
        peak,drawdown,minimum=_observe(cash,qty,avg,c[i],peak,drawdown,minimum)
        if end%DAY==16*3600000 or i==n-1:
            daily[day_count]=np.array([end,_equity(cash,qty,avg,c[i]),cash,qty[0],qty[1]]); day_count+=1
    ending=_equity(cash,qty,avg,c[-1]); duration=(ts[-1]+MINUTE-ts[0])/DAY
    stats=np.array([ending,ending-initial,drawdown,trade_count,totals[5],
        totals[5]/trade_count if trade_count else np.nan,
        totals[6]/totals[7] if totals[7]>0 else (np.inf if totals[6]>0 else np.nan),
        trade_count/duration,totals[2],totals[8],totals[9],totals[10]/trade_count if trade_count else np.nan,
        totals[11],totals[12],first_capacity,(1. if qty[0]>0 else 0.)+(1. if qty[1]>0 else 0.),ending-cash,
        minimum,totals[0]+totals[1],min_gap if totals[0]+totals[1]>1 else np.nan,totals[3],cash,
        totals[1],totals[4],ending/initial-1,maximum_qty,direction_exits])
    positions=np.empty((2,POSITION_WIDTH))
    for side in range(2):
        positions[side]=np.array([side,qty[side],avg[side],started[side],added[side],trim_qty[side],timed_out[side],first_price[side]])
    return stats,daily[:day_count],trades[:trade_count] if detail else trades,fills[:fill_count] if detail else fills,positions


def run_case(data, config, hours, seed, signals=None, run_ids=None, add_drop=0., path=0, detail=False):
    cfg=normalize_config(config)
    arrays=[np.asarray(data[key]) for key in ("ts","o","h","l","c","tp")]
    n=len(arrays[0])
    if not n or any(len(x)!=n for x in arrays): raise ValueError("双向回测数据长度不一致或为空")
    allowed=np.ones((n,2),dtype=np.bool_) if signals is None else np.asarray(signals,dtype=np.bool_)
    ids=np.full(n,-1,np.int64) if run_ids is None else np.asarray(run_ids,dtype=np.int64)
    if allowed.shape!=(n,2) or ids.shape!=(n,): raise ValueError("开仓信号与K线长度不一致")
    coins=np.random.default_rng(int(seed)).integers(0,2,n,dtype=np.uint8)
    if cfg.get("direction_rule"):
        from direction_calendar import direction_array
        directions=data.get("direction_limits")
        if directions is None:
            directions=direction_array(np.r_[arrays[0], arrays[0][-1]+MINUTE])
        if directions.shape!=(n+1,) or np.any((directions!=1)&(directions!=-1)):
            raise ValueError("方向限制日历与K线不一致")
    else:
        directions=np.zeros(1,np.int8)
    result=_simulate(*arrays,coins,allowed,ids,float(hours),cfg["initial_equity"],cfg["max_eth"],
        cfg["first_multiple"],cfg["add_multiple"],float(add_drop),cfg["entry_gap_minutes"],
        cfg["maker_fee_rate"],int(path),bool(detail),directions)
    names=STAT_NAMES+(["direction_exits"] if cfg.get("direction_rule") else [])
    stats=dict(zip(names,map(float,result[0])))
    if cfg.get("direction_rule"):
        stats["direction_pending_eth"]=float(result[4][1 if directions[-1]==1 else 0,1])
    return {"stats":stats,"daily":result[1],
            "trades":result[2],"fills":result[3],"positions":result[4]}
