"""Restore hedge case identifiers from their original request and entry catalogue."""
from __future__ import annotations

import copy
import csv
import hashlib
import itertools
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROOF_FILES = ("方案汇总.csv", "双向回测请求.json", "开仓独立比较清单.json",
               "双向运行身份.json", "行情与节假日核对.json", "已用节假日日历.json")
CORE_FILES = ("hedge_engine.py", "hedge_config.py", "hedge_signals.py", "hedge_holidays.json")
# v1.68 -> v1.69: direction filtering is opt-in; the OFF branch was replayed
# against the original data (19 cases x 10 seeds), including every fill/equity row.
# Only this exact pair of source revisions is compatible. Other core files,
# holiday data and any enabled direction rule still require exact identity.
OFF_COMPATIBLE_REVISIONS = {
    "hedge_engine.py": ("58d52f0b4d65d6f34150e40fc86465f7349abe9ced92cb40036c92ce82c52e96",
                        "93624a362066a0f7d21ceb48138fd7fc4577e0693f9a75031215e88eb875443c"),
    "hedge_config.py": ("cd5cfddf0bfc3a7f93ec81f5ad69acdc6d0f485a3389c303d1ef5b8ac7aed65d",
                        "f6dd12d6bce6b2cd61812d41e34b7e75ed4ecebb0d73ba673a4913ff406cccdb"),
}
# The independent editor now honors same-timeframe groups. Exact restorations
# already pin one descriptor and remove editor group expansion, so their
# no-expansion branch is unchanged. These are the only audited source hashes.
PINNED_SIGNAL_REVISIONS = (
    "0c6ea7fa4ba8203b98b4a4fd70c22ccbc5cb5b103bc15e3a1738781b6a93c51c",
    "e9243d4c052e6fd2734a16263721f0eddbed1af7b61f4e4275756341cee27f4c",
)


def read_json(path):
    try:
        return json.loads(Path(path).read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"缺少或无法读取双向原参数：{path}") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def core_names(request):
    return (CORE_FILES + (("hedge_entry_combinations.py",) if request.get("entry_plan") == "combined" else ())
            + (("direction_calendar.py",) if request.get("hedge",{}).get("direction_rule") else ()))


def core_hashes(names=CORE_FILES):
    return {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest() for name in names}


def require_compatible_core(request):
    original = request["hedge_exact"]["core_files_sha256"]
    current = core_hashes(core_names(request))
    if original == current:
        return
    upgraded = dict(original)
    if not request["hedge"].get("direction_rule") and all(
            original.get(name) == old and current.get(name) == new
            for name, (old, new) in OFF_COMPATIBLE_REVISIONS.items()):
        upgraded.update({name: new for name, (_, new) in OFF_COMPATIBLE_REVISIONS.items()})
    old, new = PINNED_SIGNAL_REVISIONS
    if (original.get("hedge_signals.py") == old and current.get("hedge_signals.py") == new
            and not request.get("selection", {}).get("指标组合")):
        upgraded["hedge_signals.py"] = new
    if upgraded == current:
        return
    raise ValueError("双向计算核心或节假日日历与原结果不同，不能按原指纹静默运行")


def find_matches(directory, fingerprints):
    if not (directory / "双向回测请求.json").is_file() or not (directory / "方案汇总.csv").is_file():
        return []
    matches = []
    with (directory / "方案汇总.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            fingerprint = row.get("策略标识")
            if fingerprint in fingerprints:
                matches.append(dict(kind="hedge", fingerprint=fingerprint, source_dir=str(directory), row=row,
                                    ranking_files=["方案汇总.csv"],
                                    summary=f"{directory.name}｜双向｜{row.get('开仓策略')}｜{row.get('超时平仓（小时）')}h｜{row.get('K线路径')}"))
    return matches


def narrow_selection(selection, descriptor):
    from hedge_signals import TIMEFRAMES
    narrowed = copy.deepcopy(selection)
    narrowed["开仓条件"] = {tf: [0] for tf in TIMEFRAMES}
    # A registered explicit entry group is preserved by its descriptor and registry.
    # Unused editor pools must not expand an exact case into new combinations.
    narrowed.pop("指标组合", None)
    if descriptor["id"] != "RANDOM":
        narrowed["开仓条件"] = {tf: [code] for tf, code in zip(TIMEFRAMES, descriptor["cases"])}
        narrowed["开仓指标"] = [descriptor["field"]]
        narrowed["入场触发口径"] = descriptor["entry_mode"]
    return narrowed


def resolve_exact(request, meta):
    """Validate and return one pinned descriptor and one parameter variant."""
    from hedge_config import normalize_config
    from hedge_entry_combinations import plan_request_entries
    from hedge_worker import case_identity
    from fifth_policy import require_selection_allowed
    from indicator_combinations import DEFAULT_REGISTRY

    exact = request["hedge_exact"]
    config = normalize_config(request["hedge"])
    if digest(config) != digest(request["hedge"]):
        raise ValueError("双向原配置不完整或参数被默认值替换，不能精确重测")
    require_compatible_core(request)
    if meta["request"] != exact["feature_request"] or any(
            meta.get(key) != value for key, value in exact["period"].items()):
        raise ValueError("双向行情身份或实际日期与原指纹不一致")
    if request.get("indicator_registry"):
        DEFAULT_REGISTRY.update(request["indicator_registry"])
    require_selection_allowed(request["selection"])
    descriptors, skipped = plan_request_entries(request, meta)
    descriptor = exact["descriptor"]
    if not any(digest(item) == digest(descriptor) for item in descriptors):
        reasons = "；".join(item["跳过原因"] for item in skipped)
        raise ValueError("双向原开仓规则无法按原定义还原：" + (reasons or descriptor["label"]))
    hours, drop, path = exact["hours"], exact["add_drop"], exact["path"]
    if (config["timeout_hours"] != [hours] or config["paths"] != [path]
            or (config["add_drops"] if config["mode"] == "scale_in" else [0.]) != [drop]):
        raise ValueError("双向精确列表只能运行该指纹的一组时间、补仓及K线路径参数")
    if case_identity(descriptor, config, hours, drop, path) != exact["fingerprint"]:
        raise ValueError("双向参数与策略指纹不匹配，不能换成其他参数")
    return descriptor, (hours, drop, path)


def restore_match(match, *, _fresh=None, _context=None):
    from data_sources import source_bundle_fingerprint
    from fingerprint_lookup import _same_original_row, _check_verified_snapshot
    from hedge_config import normalize_config
    from hedge_engine import ENGINE_VERSION
    from hedge_worker import case_identity

    directory = Path(match["source_dir"]).resolve()
    fingerprint = match["fingerprint"]
    fresh = find_matches(directory, {fingerprint}) if _fresh is None else _fresh.get(fingerprint, [])
    fresh = [item for item in fresh if item.get("kind") == "hedge" and Path(item["source_dir"]).resolve() == directory]
    if len(fresh) != 1:
        raise ValueError("双向原结果行已丢失或重复，请重新查找")
    row = fresh[0]["row"]
    _same_original_row(match["row"], row)
    context = {} if _context is None else _context
    if directory not in context:
        request = read_json(directory / "双向回测请求.json")
        identity = read_json(directory / "双向运行身份.json")
        meta = read_json(directory / "行情与节假日核对.json")["features"]
        catalogue = read_json(directory / "开仓独立比较清单.json")["entries"]
        sources = {key: request["sources"].get(key, "") for key in ("kline", "micro", "funding", "oi", "bundle")}
        if (sources != meta["sources"] or identity["feature_request"] != meta["request"]
                or request.get("start", "") != meta["request"]["start"]
                or request.get("end", "") != meta["request"]["end"]
                or source_bundle_fingerprint(sources) != meta["request"]["fingerprint"]):
            raise ValueError("双向原行情文件、数据身份或请求日期已变化，不能沿用该指纹")
        if identity["engine_version"] != ENGINE_VERSION:
            raise ValueError("双向原引擎版本不匹配，不能按当前模型代替原模型")
        saved_calendar = hashlib.sha256((directory / "已用节假日日历.json").read_bytes()).hexdigest()
        if saved_calendar != identity["files_sha256"]["hedge_holidays.json"]:
            raise ValueError("双向原节假日日历与运行身份不匹配")
        context[directory] = (request, identity, meta, catalogue, sources)
    original, identity, meta, catalogue, sources = context[directory]
    config = normalize_config(original["hedge"])
    if digest(config) != digest(original["hedge"]):
        raise ValueError("双向原配置缺少完整参数，不能用默认值补猜")
    variants = itertools.product(config["timeout_hours"], config["add_drops"] if config["mode"] == "scale_in" else [0.], config["paths"])
    cases = [(descriptor, hours, drop, path) for hours, drop, path in variants for descriptor in catalogue
             if case_identity(descriptor, config, hours, drop, path) == fingerprint]
    if len(cases) != 1:
        raise ValueError("双向原请求和开仓清单无法重建此指纹，未猜测缺失参数")
    descriptor, hours, drop, path = cases[0]
    expected = {"开仓策略": descriptor["label"], "开仓周期": descriptor["timeframe"],
                "开仓代码": descriptor["code"], "MACD口径": descriptor["field"],
                "入场次数口径": descriptor["entry_mode"], "超时平仓（小时）": hours,
                "补仓触发跌幅（%）": drop * 100, "K线路径": "开高低收" if path == 0 else "开低高收",
                "随机重复次数": config["seeds"]}
    if any(str(row.get(key)) != str(value) for key, value in expected.items()):
        raise ValueError("双向汇总行与原参数或随机重复次数不符，未导入不完整试验")
    exact_request = copy.deepcopy(original)
    exact_request.pop("cache_root", None)
    exact_request["hedge"] = dict(config, timeout_hours=[hours],
                                 add_drops=[drop] if config["mode"] == "scale_in" else config["add_drops"], paths=[path])
    exact_request["selection"] = narrow_selection(original["selection"], descriptor)
    exact_request["hedge_exact"] = dict(fingerprint=fingerprint, descriptor=descriptor, hours=hours,
        add_drop=drop, path=path, feature_request=meta["request"],
        core_files_sha256={name: identity["files_sha256"][name] for name in core_names(exact_request)},
        period={key: meta[key] for key in ("start_utc", "end_utc")})
    resolve_exact(exact_request, meta)
    restored = dict(kind="hedge", fingerprint=fingerprint, source_dir=str(directory),
        selection=exact_request["selection"], sources=sources, start=original.get("start", ""), end=original.get("end", ""),
        request=meta["request"], source_fingerprint=meta["request"]["fingerprint"],
        engine_version=ENGINE_VERSION, current_engine_version=ENGINE_VERSION,
        code_sha256=digest(exact_request["hedge_exact"]["core_files_sha256"]), current_code_sha256=digest(core_hashes(core_names(exact_request))),
        ranking_settings=None, period=exact_request["hedge_exact"]["period"],
        origin_identity=dict(kind="hedge", original_identity=identity, hedge_request=exact_request),
        warnings=(["已核验指定旧版兼容：保留原参数、方向限制设置及指纹；修改条件须另行回测"]
                  if exact_request["hedge_exact"]["core_files_sha256"] != core_hashes(core_names(exact_request)) else []),
        capabilities=meta.get("capabilities", {}),
        capabilities_by_timeframe=meta.get("capabilities_by_timeframe", {}))
    if "verified_snapshot" in match:
        _check_verified_snapshot(match["verified_snapshot"], restored)
    return restored


def verify_child(output, restored):
    request = read_json(output / "双向回测请求.json")
    expected = restored["origin_identity"]["hedge_request"]
    actual = {key: value for key, value in request.items() if key != "cache_root"}
    if digest(actual) != digest(expected):
        raise ValueError("双向子任务实际参数与已绑定指纹不一致")
    meta = read_json(output / "行情与节假日核对.json")["features"]
    descriptor, _ = resolve_exact(request, meta)
    if meta["sources"] != restored["sources"]:
        raise ValueError("双向子任务行情来源不一致")
    identity = read_json(output / "双向运行身份.json")
    if (identity["engine_version"] != restored["current_engine_version"]
            or identity["feature_request"] != restored["request"]
            or {name: identity["files_sha256"][name] for name in core_names(request)} != core_hashes(core_names(request))):
        raise ValueError("双向子任务计算身份不一致")
    if digest(read_json(output / "开仓独立比较清单.json")["entries"]) != digest([descriptor]):
        raise ValueError("双向子任务包含其他开仓规则")
    config = request["hedge"]
    status = read_json(output / "回测完成状态.json")
    if status != dict(completed=config["seeds"], total=config["seeds"], stopped=False):
        raise ValueError("双向子任务未完成原指纹的全部随机重复试验")
    with (output / "全部独立试验.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if (len(rows) != config["seeds"] or {int(row["种子"]) for row in rows} !=
            set(range(config["seed_start"], config["seed_start"] + config["seeds"]))
            or any(row["策略标识"] != restored["fingerprint"] for row in rows)):
        raise ValueError("双向子任务实际试验指纹或种子范围不一致")
    with (output / "方案汇总.csv").open(encoding="utf-8-sig", newline="") as stream:
        summaries = list(csv.DictReader(stream))
    if len(summaries) != 1 or summaries[0]["策略标识"] != restored["fingerprint"]:
        raise ValueError("双向子任务汇总包含其他策略")


def result_summary(output, match, restored):
    with (output / "方案汇总.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    config = restored["origin_identity"]["hedge_request"]["hedge"]
    money = float(row["期末资金中位数（USDC）"])
    original_money = float(match["row"]["期末资金中位数（USDC）"])
    original_fields_equal = all(key in row and row[key] == value for key, value in match["row"].items())
    return {"原指纹": match["fingerprint"], "本次结果指纹": row["策略标识"],
            "开仓条件": "双向独立｜" + row["开仓策略"],
            "名义倍数（倍）": config["first_multiple"] + (config["add_multiple"] if config["mode"] == "scale_in" else 0),
            "交易次数（单）": float(row["完整交易次数中位数"]), "胜率（比例，1=100%）": float(row["胜率中位数（%）"]) / 100,
            "期末资金（USDC）": money, "最大回撤（比例，1=100%）": float(row["最大回撤中位数（%）"]) / 100,
            "原结果资金（USDC）": original_money, "资金差额（USDC）": money - original_money,
            "数值对账说明": "双向重复试验中位数；" + ("原汇总已有字段全部一致" if original_fields_equal
                                                       else "原汇总字段缺失或数值存在差异")}
