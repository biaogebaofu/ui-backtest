from __future__ import annotations

import argparse
import csv
import heapq
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from backtest_worker import (中文表头, case_label, find_node, size_label,
                             排行期末资金列, 排行最大回撤列, 排行爆仓次数列,
                             最差排行名额, 应用排行设置,
                             排行行合格, 排行数值有效, 排行键, _排行状态, _排行门槛,
                             WORST_SCOPE, WORST_SCOPE_LABEL)
from export_runtime import export_xlsx_atomic
from account_statistics import CAPACITY_TIMESTAMP
from result_period import read_period_info, completed_trades_per_day
from account_statistics import ACCOUNT_FIELD, account_row
from candidate_export import (EXECUTION_COLUMNS, POSITION_COLUMNS, execution_values, position_values,
                              original_run_context, verify_run_cost_row)
from strategy_space import 生成止损组合, 生成止盈方案, 固定比例说明
from ranking_limits import top_limit
from indicator_combinations import CombinationRegistry, DEFAULT_REGISTRY, tp_spec, combination_label


def emit(kind: str, **data):
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def save_json_atomic(path: Path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def parse_size_label(value: str) -> float:
    if value.endswith("x"):
        return float(value[:-1])
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator)


def find_column(column: dict[str, int], *names: str) -> int | None:
    for name in names:
        if name in column:
            return column[name]
    return None


def export_excel(project_dir: Path, payload: Path, output: Path):
    node = find_node()
    script = project_dir / "excel_export.mjs"
    return export_xlsx_atomic(node, script, payload, output, project_dir)


def main():
    parser = argparse.ArgumentParser(description="从已有全量CSV生成各类止盈最优前5000/10000名和最差1000名")
    parser.add_argument("--output", required=True, help="已有回测结果目录")
    parser.add_argument("--source", default="", help="可直接指定任意历史的全部回测结果.csv")
    parser.add_argument("--settings", default="", help="排行榜主指标与组合门槛JSON")
    parser.add_argument("--attach-existing", action="store_true", help="把已有排行JSON写入断点，不重新扫描")
    parser.add_argument("--export-existing", action="store_true", help="只把已生成的排行JSON导出为Excel")
    parser.add_argument("--json-only", action="store_true", help="只计算原生排行JSON，不启动Excel导出")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    source = Path(args.source).resolve() if args.source else Path(args.output).resolve() / "全部回测结果.csv"
    output_dir = source.parent if args.source else Path(args.output).resolve()
    raw_settings = json.loads(Path(args.settings).read_text("utf-8-sig")) if args.settings else {}
    ranking_settings = 应用排行设置(raw_settings)
    limit = top_limit(ranking_settings)
    top_json = output_dir / "各类止盈前5000名.json"
    worst_json = output_dir / "各类止盈最差1000名.json"
    checkpoint_path = output_dir / "断点记录.json"
    if args.export_existing:
        top_payload = json.loads(top_json.read_text("utf-8"))
        worst_payload = json.loads(worst_json.read_text("utf-8"))
        available = top_limit({"最优名额": top_payload.get("名额", 5000)})
        if limit > available:
            if not source.is_file():
                raise ValueError(f"现有最优榜只保存前{available}名；缺少完整CSV，无法补足前{limit}名。")
            emit("stage", message=f"现有最优榜只保存前{available}名；将从完整CSV重建前{limit}名，不重新回测")
        else:
            export_source = top_json
            if limit != available:
                top_payload = dict(top_payload, 名额=limit,
                                   分类={name: rows[:limit] for name, rows in top_payload["分类"].items()})
                export_source = output_dir / f".top_export.{uuid.uuid4().hex}.json"
                save_json_atomic(export_source, top_payload)
            try:
                top_xlsx = export_excel(project_dir, export_source, output_dir / f"各类止盈最优前{limit}名.xlsx")
            finally:
                if export_source != top_json:
                    export_source.unlink(missing_ok=True)
            emit("legacy_excel_ready", path=str(top_xlsx))
            worst_xlsx = export_excel(project_dir, worst_json, output_dir / "各类止盈最差1000名.xlsx")
            emit("legacy_excel_ready", path=str(worst_xlsx))
            emit("legacy_completed", top_path=str(top_xlsx), worst_path=str(worst_xlsx),
                 top_limit=limit, worst_limit=最差排行名额,
                 top_rows=sum(len(x) for x in top_payload["分类"].values()),
                 worst_rows=sum(len(x) for x in worst_payload["分类"].values()), elapsed_seconds=0)
            return
    if args.attach_existing:
        checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
        top_payload = json.loads(top_json.read_text("utf-8"))
        available = top_limit({"最优名额": top_payload.get("名额", 5000)})
        if limit > available:
            raise ValueError(f"已有最优榜未保留前{limit}名，请先从完整CSV重建后再写入断点。")
        checkpoint["top"] = top_payload["分类"]
        checkpoint["top_limit"] = available
        worst_payload = json.loads(worst_json.read_text("utf-8"))
        if worst_payload.get("worst_scope") != WORST_SCOPE:
            raise ValueError("已有最差榜不是全量有效结果范围；请先从完整CSV重建排行榜，无需重跑交易。")
        checkpoint["worst"] = worst_payload["分类"]
        checkpoint["worst_scope"] = WORST_SCOPE
        save_json_atomic(checkpoint_path, checkpoint)
        emit("legacy_attached", path=str(checkpoint_path))
        return

    if not source.is_file():
        raise FileNotFoundError(f"找不到已有结果：{source}")
    run_context = original_run_context(source.parent)

    # 导出临时文件使用当前用户可写的系统临时目录。
    if not args.json_only:
        runtime_temp = Path(tempfile.gettempdir()) / "eth_backtest_export_cache" / "temp"
        runtime_temp.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = str(runtime_temp)
        os.environ["TMP"] = str(runtime_temp)

    stop_flag = output_dir / "控制" / "停止最差导出.flag"
    stop_flag.parent.mkdir(exist_ok=True)
    if stop_flag.exists():
        stop_flag.unlink()

    tps = {x.编号: x for x in 生成止盈方案()}
    stop_labels = {code: label for code, _, label in 生成止损组合()}
    registry = CombinationRegistry().load(source.parent)
    DEFAULT_REGISTRY.update(registry.to_dict())
    for identifier, _ in registry.iter_definitions('tp'):
        tps[identifier] = tp_spec(identifier, registry)
    for identifier, _ in registry.iter_definitions('stop'):
        stop_labels[identifier] = combination_label('stop', identifier, registry)
    categories_order = list(dict.fromkeys(x.类别 for x in tps.values()))
    top_heaps = {category: [] for category in categories_order}
    worst_heaps = {category: [] for category in categories_order}
    counters = {category: 0 for category in categories_order}
    period_days, _period_years = read_period_info(output_dir)
    total_bytes = source.stat().st_size
    bytes_read = rows_read = 0
    last_emit = time.time()
    started = last_emit
    payload_initial_capital = 100.0

    emit("legacy_start", csv=str(source), bytes=total_bytes, categories=len(categories_order))
    with source.open("rb", buffering=8 * 1024 * 1024) as handle:
        header = handle.readline()
        bytes_read += len(header)
        header_names = next(csv.reader([header.decode("utf-8-sig").rstrip("\r\n")]))
        column = {name: index for index, name in enumerate(header_names)}

        cooldown_idx = column.get("止盈后等待分钟")
        size_order_idx = find_column(column, "所选仓位顺序", "22档仓位顺序")
        finals_idx = find_column(column, "所选仓位期末资金（USDC）", "所选仓位期末资金", "22档期末资金")
        returns_idx = find_column(column, "所选仓位累计收益率（%）", "所选仓位累计收益率", "22档累计收益率")
        drawdowns_idx = find_column(column, "所选仓位最大回撤（%）", "所选仓位最大回撤", "22档最大回撤")
        liquidations_idx = find_column(column, "所选仓位爆仓保护次数（次）", "所选仓位爆仓保护次数", "22档爆仓保护次数")
        forced_idx = column.get("所选仓位全仓强平次数（次）")
        fixed_stop_idx = column.get("固定止损代码")
        direction_idx = column.get("开仓方向")
        session_idx = column.get("交易会话")
        hard_idx = column.get("强制时间止损（分钟）")
        entry_mode_idx = column.get("入场触发口径")
        account_stats_idx = column.get(ACCOUNT_FIELD)
        engine_version_idx = column.get("回测计算版本")
        overlay_idx = column.get("叠加止盈代码")
        entry_slippage_idx = find_column(column, "开仓成交偏移（%）")
        exit_slippage_idx = find_column(column, "平仓成交偏移（%）")
        roundtrip_slippage_idx = find_column(column, "往返成交偏移（%）")
        initial_capital_idx = find_column(column, "初始资金（USDC）")
        minimum_eth_idx = find_column(column, "ETH最小开仓数量（ETH）")
        maximum_eth_idx = find_column(column, "ETH单次最大开仓数量（ETH）")
        executed_idx = find_column(column, "所选仓位实际成交次数（单）")
        capital_stop_idx = find_column(column, "所选仓位资金性停机标记（0否1是）")
        end_quantity_idx = find_column(column, "所选仓位期末可开仓数量（ETH）")
        required = (size_order_idx, finals_idx, returns_idx, drawdowns_idx, liquidations_idx)
        if any(index is None for index in required):
            raise ValueError("已有CSV缺少仓位结果列，无法生成旧版排行")

        def col(*names: str) -> int:
            index = find_column(column, *names)
            if index is None:
                raise ValueError(f"已有CSV缺少列：{' / '.join(names)}")
            return index

        for raw in handle:
            bytes_read += len(raw)
            rows_read += 1
            comma = raw.find(b",")
            if comma <= 0:
                continue
            tp_id = int(raw[:comma])
            tp = tps.get(tp_id)
            if tp is None:
                continue
            parts = raw.rstrip(b"\r\n").split(b",")
            if len(parts) < 25:
                continue
            finals = [float(x) for x in parts[finals_idx].split(b";")]
            top_heap = top_heaps[tp.类别]
            worst_heap = worst_heaps[tp.类别]
            # 主排行指标和组合门槛可以来自任意导出列，必须检查每个仓位档；
            # 不能再只用期末资金提前裁剪，否则会漏掉低回撤等排行候选。
            candidate_indices = range(len(finals))
            if candidate_indices:
                returns = [float(x) for x in parts[returns_idx].split(b";")]
                drawdowns = [float(x) for x in parts[drawdowns_idx].split(b";")]
                liquidations = [int(x) for x in parts[liquidations_idx].split(b";")]
                # 旧CSV没有这一列，按0补齐以保持行宽一致。
                forced_raw = parts[forced_idx].strip() if forced_idx is not None else b""
                forced = ([int(x) for x in forced_raw.split(b";")]
                          if forced_raw else [0] * len(liquidations))
                size_labels = parts[size_order_idx].decode("utf-8").split(";")
                raw_trades = int(parts[col("交易次数（单）", "交易次数")])
                initial_capital = float(parts[initial_capital_idx]) if initial_capital_idx is not None else 100.0
                minimum_eth = float(parts[minimum_eth_idx]) if minimum_eth_idx is not None else 0.0
                maximum_eth = float(parts[maximum_eth_idx]) if maximum_eth_idx is not None else 100.0
                payload_initial_capital = initial_capital
                executed = ([int(x) for x in parts[executed_idx].split(b";")]
                            if executed_idx is not None else [raw_trades] * len(finals))
                capital_stops = ([int(x) for x in parts[capital_stop_idx].split(b";")]
                                 if capital_stop_idx is not None else [0] * len(finals))
                end_quantities = ([float(x) for x in parts[end_quantity_idx].split(b";")]
                                  if end_quantity_idx is not None else [0.0] * len(finals))
                base_id = int(parts[column["基础策略编号"]])
                field = int(parts[column["开仓MACD代码"]])
                cases = [int(parts[column[name]]) for name in
                         ("4小时条件代码", "1小时条件代码", "15分钟条件代码", "5分钟条件代码", "1分钟条件代码")]
                stop_code = parts[column["止损代码"]].decode("utf-8")
                # 旧CSV没有固定止损列，按OFF补齐以保持行宽一致。
                fixed_code = (parts[fixed_stop_idx].decode("utf-8")
                              if fixed_stop_idx is not None else "OFF")
                fixed_label = 固定比例说明(fixed_code, "FSL")
                # 旧CSV没有这三列，按中性值补齐以保持行宽与中文表头一致。
                direction = (parts[direction_idx].decode("utf-8")
                             if direction_idx is not None else "BOTH")
                session = (parts[session_idx].decode("utf-8")
                           if session_idx is not None else "ALL")
                hard_raw = parts[hard_idx].strip() if hard_idx is not None else b""
                hard_min = int(hard_raw) if hard_raw else 0
                entry_mode = (parts[entry_mode_idx].decode("utf-8")
                              if entry_mode_idx is not None else "LEGACY_UNKNOWN")
                # 旧CSV没有这列，按 OFF 补齐保持行宽一致。
                overlay = (parts[overlay_idx].decode("utf-8")
                           if overlay_idx is not None else "OFF")
                overlay_label = 固定比例说明(overlay, "FTP")
                cooldown = int(parts[cooldown_idx]) if cooldown_idx is not None else 0
                entry_slippage = float(parts[entry_slippage_idx]) if entry_slippage_idx is not None else 0.0
                exit_slippage = float(parts[exit_slippage_idx]) if exit_slippage_idx is not None else 0.0
                roundtrip_slippage = (float(parts[roundtrip_slippage_idx]) if roundtrip_slippage_idx is not None
                                      else entry_slippage + exit_slippage)
                execution = execution_values({name: parts[column[name]].decode("utf-8")
                                              for name in EXECUTION_COLUMNS if name in column})
                position = position_values({name: parts[column[name]].decode("utf-8")
                                            for name in POSITION_COLUMNS if name in column})
                old_daily_orders = find_column(column, "平均日成交单数（单/日）", "平均日成交单数")
                daily_orders_idx = find_column(column, "平均日成交订单数（笔/日）")
                daily_trades_idx = find_column(column, "平均日完整交易数（次/日）", "平均日完整交易数（单/日）")
                daily_orders = float(parts[daily_orders_idx if daily_orders_idx is not None else old_daily_orders])
                daily_row = {"交易次数（单）": raw_trades,
                             "平均日成交订单数（笔/日）": daily_orders}
                if daily_trades_idx is not None:
                    daily_row["平均日完整交易数（次/日）"] = parts[daily_trades_idx].decode("utf-8")
                daily_trades = completed_trades_per_day(daily_row, tp.类别, period_days)
                fixed = [
                    raw_trades, float(parts[col("胜率（%）", "胜率")]), float(parts[col("多单占比（%）", "多单占比")]),
                    daily_trades, daily_orders,
                    float(parts[col("平均持仓时间（分钟）", "平均持仓分钟")]),
                    float(parts[col("平均单笔收益率（%）", "平均单笔收益率")]),
                    float(parts[col("毛收益合计（%）", "毛收益合计")]),
                    float(parts[col("盈亏比（倍）", "盈亏比")]), float(parts[column["t值"]]),
                    float(parts[col("2025毛收益（%）", "2025毛收益")]),
                    float(parts[col("2026毛收益（%）", "2026毛收益")]),
                ]
                for size_i in candidate_indices:
                    if size_i >= min(len(returns), len(drawdowns), len(liquidations), len(size_labels),
                                     len(executed), len(capital_stops), len(end_quantities)):
                        continue
                    mult = parse_size_label(size_labels[size_i])
                    actual = None
                    if account_stats_idx is not None and parts[account_stats_idx].strip():
                        actual = account_row({ACCOUNT_FIELD: parts[account_stats_idx].decode("utf-8")}, size_i)
                    size_fixed = fixed if actual is None else [float(actual[name]) for name in (
                        "交易次数（单）", "胜率（%）", "多单占比（%）", "平均日完整交易数（次/日）",
                        "平均日成交订单数（笔/日）", "平均持仓时间（分钟）", "平均单笔收益率（%）",
                        "毛收益合计（%）", "盈亏比（倍）", "t值", "2025毛收益（%）", "2026毛收益（%）")]
                    row = [
                        tp.编号, tp.类别, tp.周期组合, tp.指标, tp.参数一, tp.参数二, tp.参数三,
                        cooldown, base_id, "MACD柱" if field == 0 else "DIF线",
                        *(case_label(x) for x in cases), entry_mode, stop_code, stop_labels.get(stop_code, stop_code),
                        fixed_code, fixed_label, overlay, overlay_label,
                        direction, session, hard_min,
                        size_label(mult), mult,
                        *size_fixed[:10],
                        finals[size_i], returns[size_i], drawdowns[size_i], liquidations[size_i],
                        forced[size_i] if size_i < len(forced) else 0, size_fixed[10], size_fixed[11],
                        entry_slippage, exit_slippage, roundtrip_slippage,
                        (parts[engine_version_idx].decode("utf-8") if engine_version_idx is not None
                         else "旧版（未逐仓重放）"),
                        initial_capital, minimum_eth, maximum_eth,
                        executed[size_i], capital_stops[size_i], min(end_quantities[size_i], maximum_eth),
                        (float(actual["实际止盈等待总时间（分钟）"])
                         if actual and "实际止盈等待总时间（分钟）" in actual else None),
                        (float(actual["实际容量占用率（%）"])
                         if actual and "实际容量占用率（%）" in actual else None),
                        (int(parts[column["ETH最小开仓约束启用（0否1是）"]])
                         if "ETH最小开仓约束启用（0否1是）" in column
                         and parts[column["ETH最小开仓约束启用（0否1是）"]].strip() else None),
                        parts[column["成交价格口径"]].decode("utf-8") if "成交价格口径" in column else "THEORETICAL",
                        *execution.values(), *position.values(),
                        float(actual.get(CAPACITY_TIMESTAMP, -2)) if actual else -2,
                    ]
                    verify_run_cost_row(dict(zip(中文表头, row)), run_context)
                    if not 排行数值有效(row):
                        continue
                    score = 排行键(row)
                    counters[tp.类别] += 1
                    counter = counters[tp.类别]
                    if 排行行合格(row):
                        if len(top_heap) < limit:
                            heapq.heappush(top_heap, (score, counter, row))
                        elif score > top_heap[0][0]:
                            heapq.heapreplace(top_heap, (score, counter, row))
                    if len(worst_heap) < 最差排行名额:
                        heapq.heappush(worst_heap, (-score, counter, row))
                    elif score < -worst_heap[0][0]:
                        heapq.heapreplace(worst_heap, (-score, counter, row))

            now = time.time()
            if now - last_emit >= 2.0:
                speed = bytes_read / max(0.001, now - started)
                emit("legacy_progress", rows=rows_read, bytes=bytes_read,
                     percent=bytes_read / total_bytes * 100.0,
                     eta_seconds=(total_bytes - bytes_read) / max(1.0, speed))
                last_emit = now
                if stop_flag.exists():
                    emit("legacy_stopped", rows=rows_read)
                    return

    top_categories = {
        category: [item[2] for item in sorted(heap, key=lambda x: (x[0], -float(x[2][排行最大回撤列])), reverse=True)]
        for category, heap in top_heaps.items() if heap
    }
    worst_categories = {
        category: [item[2] for item in sorted(heap, key=lambda x: (排行键(x[2]),
                                                                   -float(x[2][排行最大回撤列]),
                                                                   -int(x[2][排行爆仓次数列])))]
        for category, heap in worst_heaps.items() if heap
    }
    context_payload = {'运行上下文': run_context['runtime_context']} if run_context is not None else {}
    context_payload.update(worst_scope=WORST_SCOPE, 最差排行范围=WORST_SCOPE_LABEL)
    save_json_atomic(top_json, {"表头": 中文表头, "分类": top_categories, "排行": "最优", "名额": limit,
                                  **context_payload,
                                  "初始资金": payload_initial_capital,
                                  "排行指标": _排行状态["名称"],
                                  "组合门槛": [{"指标": x["指标"], "条件": x["条件"], "值": x["值"]}
                                               for x in _排行门槛]})
    save_json_atomic(worst_json, {"表头": 中文表头, "分类": worst_categories, "排行": "最差", "名额": 最差排行名额,
                                    **context_payload,
                                    "初始资金": payload_initial_capital,
                                    "排行指标": _排行状态["名称"],
                                    "组合门槛": [{"指标": x["指标"], "条件": x["条件"], "值": x["值"]}
                                                 for x in _排行门槛]})
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
        checkpoint["top"] = top_categories
        checkpoint["top_limit"] = limit
        checkpoint["worst"] = worst_categories
        checkpoint["worst_scope"] = WORST_SCOPE
        save_json_atomic(checkpoint_path, checkpoint)
    if "最优名额" in ranking_settings:
        settings_path = output_dir / "排行榜设置.json"
        existing_settings = json.loads(settings_path.read_text("utf-8-sig")) if settings_path.exists() else None
        if existing_settings != ranking_settings:
            save_json_atomic(settings_path, ranking_settings)
    emit("legacy_json_ready", top=str(top_json), worst=str(worst_json), elapsed_seconds=time.time() - started)

    if args.json_only:
        emit("legacy_json_completed", top_limit=limit, worst_limit=最差排行名额,
             top_rows=sum(len(x) for x in top_categories.values()), worst_rows=sum(len(x) for x in worst_categories.values()))
        return

    top_xlsx = export_excel(project_dir, top_json, output_dir / f"各类止盈最优前{limit}名.xlsx")
    emit("legacy_excel_ready", path=str(top_xlsx))
    worst_xlsx = export_excel(project_dir, worst_json, output_dir / "各类止盈最差1000名.xlsx")
    emit("legacy_excel_ready", path=str(worst_xlsx))
    emit("legacy_completed", top_path=str(top_xlsx), worst_path=str(worst_xlsx),
         top_limit=limit, worst_limit=最差排行名额,
         top_rows=sum(len(x) for x in top_categories.values()),
         worst_rows=sum(len(x) for x in worst_categories.values()),
         elapsed_seconds=time.time() - started)


if __name__ == "__main__":
    main()
