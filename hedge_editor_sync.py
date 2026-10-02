"""Project verified hedge fingerprints into editable entry and holding controls."""
from __future__ import annotations

import copy

from hedge_config import normalize_config
from hedge_fingerprints import digest
from hedge_signals import TIMEFRAMES
from indicator_combinations import CombinationRegistry, DEFAULT_REGISTRY
from fifth_policy import require_selection_allowed
from selection_config import 规范化入场触发口径, 每个组合含第五批
from extended_rules import FIFTH_ROUND_CODES


def merge_hedge_restorations(restorations):
    if not restorations or any(item.get("kind") != "hedge" for item in restorations):
        raise ValueError("单仓与双向参数不能合并到同一编辑页；请按原指纹精确列表运行")
    requests = [item["origin_identity"]["hedge_request"] for item in restorations]
    configs = [normalize_config(request["hedge"]) for request in requests]
    varying = {"timeout_hours", "add_drops", "paths"}
    labels = {"mode": "持仓模式", "initial_equity": "本金", "max_eth": "每方向数量上限",
              "tp_weekday": "工作日止盈", "tp_holiday": "周末/假日止盈", "entry_gap_minutes": "开仓间隔",
              "maker_fee_rate": "Maker手续费", "seeds": "随机重复次数", "seed_start": "起始随机种子",
              "direction_rule": "红绿中点方向限制"}
    first = restorations[0]
    plan = requests[0].get("entry_plan", "independent")
    registry = CombinationRegistry()
    for index, (item, request, config) in enumerate(zip(restorations, requests, configs), 1):
        for key in ("sources", "start", "end", "request", "period", "code_sha256", "current_code_sha256"):
            if digest(item.get(key)) != digest(first.get(key)):
                raise ValueError(f"第{index}条数据、日期或计算定义不同，不能同步到同一编辑组合；精确列表仍保留原参数")
        if request.get("entry_plan", "independent") != plan:
            raise ValueError("单周期对照与多周期组合不能共用同一个编辑设置；请使用精确列表")
        if request["selection"]["入场约束"] != requests[0]["selection"]["入场约束"]:
            raise ValueError("指纹之间的S3入场约束不同，不能合并编辑")
        for key in configs[0].keys() | config.keys():
            if key not in varying and config.get(key) != configs[0].get(key):
                raise ValueError(f"第{index}条{labels.get(key, key)}不同，编辑页只能填写一组；请使用原指纹精确列表")
        if request.get("indicator_registry"):
            registry.update(request["indicator_registry"])
        require_selection_allowed(request["selection"], registry)
    descriptors = [request["hedge_exact"]["descriptor"] for request in requests]
    entries = [entry for entry in descriptors if entry["id"] != "RANDOM"]
    if not entries and configs[0]["compare_entries"]:
        raise ValueError("该随机基线使用了开仓对照的200根预热，不能改成普通随机开仓；请使用原指纹精确列表")
    modes = list(dict.fromkeys(entry["entry_mode"] for entry in entries)) or ["LIVE_01"]
    if any(mode != "F5_EVENT" for mode in modes):
        modes = [mode for mode in modes if mode != "F5_EVENT"]
    selection = {"开仓条件": {tf: list(dict.fromkeys(entry["cases"][i] for entry in entries)) or [0]
                              for i, tf in enumerate(TIMEFRAMES)},
                 "开仓指标": list(dict.fromkeys(entry["field"] for entry in entries)) or ["hist"],
                 "入场触发口径": 规范化入场触发口径(modes),
                 "入场约束": copy.deepcopy(requests[0]["selection"]["入场约束"]), "指标组合": {}}
    for tf, codes in selection["开仓条件"].items():
        if any(code < 0 for code in codes):
            if len(codes) != 1:
                raise ValueError(f"{tf}含不同复合条件，不能无损合并勾选；请使用原指纹精确列表")
            definition = registry.resolve("entry", codes[0])
            if definition is None:
                raise ValueError(f"{tf}缺少原复合条件字典，不能猜测成员")
            selection["开仓条件"][tf] = definition["members"]
            selection["指标组合"].setdefault("开仓", {})[tf] = dict(
                启用=True, 组合数量=[len(definition["members"])], 保留单项=False, 逻辑=definition["logic"])
    require_selection_allowed(selection, registry)
    if len(modes) > 1 and any(code in FIFTH_ROUND_CODES for codes in selection["开仓条件"].values() for code in codes):
        raise ValueError("包含第五批时，编辑页只支持一种普通入场口径；多种口径请使用原指纹精确列表")
    if modes == ["F5_EVENT"] and not 每个组合含第五批(selection):
        raise ValueError("合并后的部分交叉组合不含第五批，无法共用第五批事件口径；请使用原指纹精确列表")
    config = copy.deepcopy(configs[0])
    for key in varying:
        config[key] = sorted({value for item in configs for value in item[key]})
    from hedge_panel import DEFAULT_FIELDS, fields_to_config
    number = lambda value: format(value, ".15g")
    fields = dict(DEFAULT_FIELDS, mode=config["mode"], initial_equity=number(config["initial_equity"]),
        max_eth=number(config["max_eth"]), add_drops_percent=",".join(number(value * 100) for value in config["add_drops"]),
        tp_weekday_percent=number(config["tp_weekday"] * 100), tp_holiday_percent=number(config["tp_holiday"] * 100),
        timeout_hours=",".join(number(value) for value in config["timeout_hours"]),
        entry_gap_minutes=str(config["entry_gap_minutes"]), maker_fee_percent=number(config["maker_fee_rate"] * 100),
        seeds=str(config["seeds"]), seed_start=str(config["seed_start"]), compare_entries=config["compare_entries"],
        paths="both" if config["paths"] == [0, 1] else str(config["paths"][0]), entry_plan=plan,
        direction_limit=bool(config.get("direction_rule")))
    if fields_to_config(fields) != config:
        raise ValueError("原双向参数无法通过当前输入框精确表示，请使用原指纹列表")
    # Enumerate only the small projected entry selection to disclose extra cases.
    from hedge_entry_combinations import plan_request_entries
    DEFAULT_REGISTRY.update(registry.to_dict())
    projected_selection = dict(copy.deepcopy(requests[0]["selection"]), **selection)
    projected_request = dict(selection=projected_selection, hedge=config, entry_plan=plan)
    metadata = dict(capabilities=first["capabilities"], capabilities_by_timeframe=first.get("capabilities_by_timeframe", {}))
    planned, _ = plan_request_entries(projected_request, metadata)
    if not {entry["id"] for entry in descriptors}.issubset({entry["id"] for entry in planned}):
        raise ValueError("合并后有原开仓条件未被保留，未修改编辑参数")
    variants = len(config["timeout_hours"]) * len(config["paths"]) * (len(config["add_drops"]) if config["mode"] == "scale_in" else 1)
    entry_count = sum(entry["id"] != "RANDOM" for entry in planned)
    projected_count = entry_count * variants
    original_count = len({item["fingerprint"] for item, descriptor in zip(restorations, descriptors) if descriptor["id"] != "RANDOM"})
    return dict(selection=selection, fields=fields,
                **{key: copy.deepcopy(first[key]) for key in ("sources", "start", "end", "capabilities")},
                capabilities_by_timeframe=copy.deepcopy(first.get("capabilities_by_timeframe", {})),
                exact_count=len(restorations), entry_count=entry_count, projected_count=projected_count,
                baseline_count=variants, extra_count=projected_count - original_count)
