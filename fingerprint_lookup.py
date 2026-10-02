"""Read original records; validate the permanent policy when binding or applying them."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from account_statistics import ENGINE_VERSION
from data_sources import source_bundle_fingerprint
from execution_settings import cost_modes, normalize_cost_mode, effective_fee_rates, effective_slippage
from extended_rules import stable_base_id, unavailable_entry_codes, capabilities_for_timeframe
from ranking_view import CODE_NAMES, config_fingerprint
from ranking_limits import top_limit
from run_safety import build_run_identity
from selection_config import 规范化配置, 规范化入场触发口径, 入场口径列表, 结果入场口径列表, 配置签名, 配置统计, 排行指标选项
from strategy_space import 生成止损组合
from indicator_combinations import (DEFAULT_REGISTRY, CombinationRegistry, choices_for_config,
                                    combination_label)


RANKING_FILES = ("各类止盈前5000名.json", "各类止盈最差1000名.json")
CANDIDATE_FILES = ("候选原始记录.json",)
SOURCE_KEYS = ("kline", "micro", "funding", "oi", "bundle")
CASE_FIELDS = (("4h", "4小时条件"), ("1h", "1小时条件"),
               ("15m", "15分钟条件"), ("5m", "5分钟条件"), ("1m", "1分钟条件"))
SINGLE_FIELDS = {
    "止损代码": "止损代码", "固定止损代码": "固定止损代码", "叠加止盈代码": "叠加止盈代码",
    "开仓方向": "开仓方向", "交易会话": "交易会话", "强制时间止损分钟": "强制时间止损（分钟）",
    "止盈方案编号": "止盈方案编号", "止盈后等待分钟": "止盈后等待分钟", "仓位倍数": "名义倍数（倍）",
}
SNAPSHOT_FIELDS = (
    "selection", "sources", "start", "end", "request", "source_fingerprint", "code_sha256",
    "engine_version", "ranking_settings", "source_dir", "fingerprint", "origin_identity",
)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取原始结果文件：{path}\n{exc}") from exc


def _fingerprint(value):
    text = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", text):
        raise ValueError("请输入排行榜或新版候选的16位策略指纹；旧候选20位指纹请先重新生成候选")
    return text


def parse_fingerprints(text: str) -> list[str]:
    """Parse whole tokens only; preserve first occurrence order after normalization."""
    if not isinstance(text, str):
        raise ValueError("策略指纹必须是文本")
    tokens = [token for token in re.split(r"[\s,，;；]+", text.strip()) if token]
    if not tokens:
        raise ValueError("请至少输入一个16位策略指纹")
    result = []
    seen = set()
    for token in tokens:
        fingerprint = _fingerprint(token)
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(fingerprint)
    return result


def _is_result_dir(path):
    return any((path / name).is_file() for name in
               (*RANKING_FILES, *CANDIDATE_FILES, "组合选择.json", "断点记录.json", "回测数据说明.json", "双向回测请求.json"))


def _search_directories(root):
    if _is_result_dir(root):
        return [root]
    def is_collection(path):
        return any((path / name).is_file() for name in ('批量状态.json', '候选完整参数.json'))

    def real_children(path):
        return sorted(child for child in path.iterdir()
                      if child.is_dir() and not child.is_symlink()
                      and not (getattr(child.lstat(), 'st_file_attributes', 0)
                               & stat.FILE_ATTRIBUTE_REPARSE_POINT))

    candidates = [root] if is_collection(root) else real_children(root)
    directories = []
    for path in candidates:
        if _is_result_dir(path):
            directories.append(path)
        elif is_collection(path):
            # Only recognized collections get one extra level. Never follow
            # saved source paths, links, cache trees or arbitrary nested folders.
            directories.extend(child for child in real_children(path) if _is_result_dir(child))
    return directories


def find_fingerprint_matches(fingerprint: str, search_root: Path) -> list[dict]:
    """Search one result directory or its root's direct children, without reading market data."""
    fingerprint = _fingerprint(fingerprint)
    return find_fingerprint_groups(fingerprint, search_root)[fingerprint]


def find_fingerprint_groups(text: str, search_root: Path) -> dict[str, list[dict]]:
    """Read each original ranking once for the complete ordered fingerprint query."""
    fingerprints = parse_fingerprints(text)
    root = Path(search_root).resolve()
    if not root.is_dir():
        raise ValueError(f"结果目录不存在：{root}")
    directories = _search_directories(root)
    groups = {fingerprint: [] for fingerprint in fingerprints}
    for directory in directories:
        from hedge_fingerprints import find_matches as find_hedge_matches
        for match in find_hedge_matches(directory, groups):
            groups[match["fingerprint"]].append(match)
        matches = {}
        for name in (*RANKING_FILES, *CANDIDATE_FILES):
            path = directory / name
            if not path.is_file():
                continue
            payload = _read_json(path)
            headers = payload.get("表头") if isinstance(payload, dict) else None
            categories = payload.get("分类") if isinstance(payload, dict) else None
            if (not isinstance(headers, list) or not all(isinstance(h, str) for h in headers)
                    or len(set(headers)) != len(headers) or not isinstance(categories, dict)
                    or "基础策略编号" not in headers or "名义倍数（倍）" not in headers):
                raise ValueError(f"需要未经视图转换的原始排行榜JSON：{path}")
            for rows in categories.values():
                if not isinstance(rows, list):
                    raise ValueError(f"原排行榜分类格式错误：{path}")
                for values in rows:
                    if not isinstance(values, list) or len(values) != len(headers):
                        raise ValueError(f"原排行榜行宽与表头不一致：{path}")
                    row = dict(zip(headers, values))
                    fingerprint = config_fingerprint(row)
                    if fingerprint not in groups:
                        continue
                    if name in CANDIDATE_FILES:
                        origin = payload.get("原始结果目录")
                        if (not isinstance(origin, str) or not origin.strip()
                                or Path(origin).resolve() != directory):
                            raise ValueError("候选原始记录的来源目录与当前目录不一致；请在原结果目录重新生成候选，不能套用其他任务的运行配置")
                    if fingerprint not in matches:
                        matches[fingerprint] = {"fingerprint": fingerprint, "source_dir": str(directory),
                                 "row": row, "ranking_files": [],
                                 "summary": f"{directory.name}｜{row.get('1分钟条件', '')}｜"
                                            f"止盈#{row.get('止盈方案编号')}｜{row.get('名义倍数（倍）')}x"}
                    if name not in matches[fingerprint]["ranking_files"]:
                        matches[fingerprint]["ranking_files"].append(name)
        for fingerprint, match in matches.items():
            groups[fingerprint].append(match)
    return groups


def restore_many(matches: list[dict], progress=None) -> list[dict]:
    """Return the complete verified batch or raise; never expose a partial result."""
    if not isinstance(matches, list) or not matches:
        raise ValueError("请至少选择一个原始策略记录")
    by_directory = {}
    for match in matches:
        fingerprint, directory = _match_record(match)
        by_directory.setdefault(directory, []).append(fingerprint)
    from hedge_fingerprints import PROOF_FILES
    ranking_paths = [directory / name for directory in by_directory
                     for name in (*RANKING_FILES, *CANDIDATE_FILES, *PROOF_FILES)]
    def versions():
        result = []
        for path in ranking_paths:
            stat = path.stat() if path.is_file() else None
            result.append((path, (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if stat else None))
        return result
    original_versions = versions()
    fresh = {}
    for directory, fingerprints in by_directory.items():
        if progress:
            progress(f"正在读取原始排行榜：{directory.name}……")
        fresh[directory] = find_fingerprint_groups(" ".join(fingerprints), directory)
    restored = []
    hedge_context = {}
    for index, match in enumerate(matches, 1):
        if progress:
            progress(f"正在核验指纹 {index}/{len(matches)}：{match['fingerprint']}；全部通过后加入列表……")
        restored.append(restore_fingerprint_match(match, _fresh=fresh[Path(match["source_dir"]).resolve()],
                                                   _hedge_context=hedge_context))
    if versions() != original_versions:
        raise ValueError("核验期间原排行榜已变化，请完成原任务后重新核验；未加入任何指纹")
    return restored


def merge_fingerprint_restorations(restorations: list[dict]) -> dict:
    """Project verified independent strategies into editor checkbox unions.

    Only declared combination dimensions may differ. Shared runtime settings
    must agree; the returned count makes additional Cartesian combinations explicit.
    This is an editor projection, not a replacement for any pinned strategy.
    """
    if not isinstance(restorations, list) or not restorations:
        raise ValueError("请先核验至少一个指纹再同步到编辑组合")
    if any(item.get("kind") == "hedge" for item in restorations):
        raise ValueError("双向持仓参数请用指纹精确列表独立回测，不能并入单仓编辑组合")
    common = {
        "sources": "数据来源", "start": "开始日期", "end": "结束日期",
        "request": "数据请求", "source_fingerprint": "数据指纹", "period": "实际数据期间",
        "capabilities": "数据能力", "ranking_settings": "排行榜设置",
        "engine_version": "原计算版本", "code_sha256": "原计算代码",
        "current_engine_version": "当前计算版本", "current_code_sha256": "当前计算代码",
    }
    dimensions = (*SINGLE_FIELDS, "开仓指标", "开仓位置过滤")
    projected_fields = {*dimensions, "开仓条件", "入场触发口径", "成本模式", "指标组合"}
    configs = []
    for index, item in enumerate(restorations, 1):
        _require_keys(item, (*common, "selection", "fingerprint", "source_dir", "warnings"), "待合并的核验结果")
        _fingerprint(item["fingerprint"])
        config = _complete_selection(item["selection"])
        from fifth_policy import require_selection_allowed
        require_selection_allowed(config)
        if 配置统计(config)["包含仓位完整组合数"] != 1:
            raise ValueError(f"第{index}条指纹不是单一组合，不能同步")
        if configs:
            if item.get("capabilities_by_timeframe", {}) != restorations[0].get("capabilities_by_timeframe", {}):
                raise ValueError(f"第{index}条的各周期数据能力不同，不能合并到同一编辑组合；请使用指纹精确列表")
            for key, label in common.items():
                actual, expected = item[key], restorations[0][key]
                if key == "ranking_settings" and isinstance(actual, dict) and isinstance(expected, dict):
                    actual = {**actual, "最优名额": top_limit(actual)}
                    expected = {**expected, "最优名额": top_limit(expected)}
                if _snapshot_digest(actual) != _snapshot_digest(expected):
                    raise ValueError(f"第{index}条的{label}不同，不能合并到同一编辑组合；请使用指纹精确列表")
            for key in configs[0].keys() - projected_fields:
                if _snapshot_digest(config[key]) != _snapshot_digest(configs[0][key]):
                    raise ValueError(f"第{index}条的{key}不同，不能合并到同一编辑组合；请使用指纹精确列表")
        configs.append(config)
    # Pool unions would turn two exact indicator groups into additional groups.
    # Keep that operation in the already supported independent strategy queue.
    if any(config.get("指标组合") for config in configs):
        groups = [{"指标组合": config.get("指标组合"), "开仓条件": config["开仓条件"],
                   "止损代码": config["止损代码"], "止盈方案编号": config["止盈方案编号"]}
                  for config in configs]
        if any(group != groups[0] for group in groups[1:]):
            raise ValueError("不同指标组合不能合并指标池，否则会产生未选择的组合；请使用指纹精确列表")
    merged = copy.deepcopy(restorations[0])
    selection = copy.deepcopy(configs[0])
    for key in dimensions:
        selection[key] = list(dict.fromkeys(value for config in configs for value in config[key]))
    for timeframe, _ in CASE_FIELDS:
        selection["开仓条件"][timeframe] = list(dict.fromkeys(
            value for config in configs for value in config["开仓条件"][timeframe]))
    selection["入场触发口径"] = 规范化入场触发口径(list(dict.fromkeys(
        mode for config in configs for mode in 入场口径列表(config))))
    selection["成本模式"] = normalize_cost_mode(list(dict.fromkeys(
        mode for config in configs for mode in cost_modes(config))))
    merged["selection"] = 规范化配置(selection)
    merged["exact_count"] = len(restorations)
    merged["unique_count"] = len({配置签名(config) for config in configs})
    merged["projected_count"] = 配置统计(merged["selection"])["包含仓位完整组合数"]
    merged["extra_count"] = merged["projected_count"] - merged["unique_count"]
    merged["fingerprints"] = [item["fingerprint"] for item in restorations]
    merged["source_dirs"] = list(dict.fromkeys(item["source_dir"] for item in restorations))
    merged["warnings"] = list(dict.fromkeys(warning for item in restorations for warning in item["warnings"]))
    return merged


def _match_record(match):
    _require_keys(match, ("fingerprint", "source_dir", "row"), "指纹匹配")
    if (not isinstance(match["fingerprint"], str) or not isinstance(match["source_dir"], str)
            or not match["source_dir"].strip() or not isinstance(match["row"], dict)):
        raise ValueError("指纹匹配必须保留完整原始行和明确来源，请重新查找")
    fingerprint = _fingerprint(match["fingerprint"])
    actual = match["row"].get("策略标识") if match.get("kind") == "hedge" else config_fingerprint(match["row"])
    if actual != fingerprint:
        raise ValueError("队列原始行与策略指纹不匹配；请移除后重新核验，不能替换为另一行")
    return fingerprint, Path(match["source_dir"]).resolve()


def _same_original_row(saved, fresh):
    if saved.keys() != fresh.keys():
        raise ValueError("队列原始行字段与当前来源不一致；请移除后重新核验")
    for key in saved:
        try:
            _same(saved[key], fresh[key], key)
        except ValueError as exc:
            raise ValueError(f"队列原始行已变化：{key}；请移除后重新核验，原记录不会被替换") from exc


def _snapshot_digest(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("已核验快照包含非法数据，请移除后重新核验") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _restoration_snapshot(restored):
    _require_keys(restored, SNAPSHOT_FIELDS, "已核验的原参数")
    snapshot = {"version": 1, **copy.deepcopy({key: restored[key] for key in SNAPSHOT_FIELDS})}
    snapshot["identity_sha256"] = _snapshot_digest(snapshot)
    return snapshot


def _check_verified_snapshot(saved, restored):
    required = {"version", "identity_sha256", *SNAPSHOT_FIELDS}
    if (not isinstance(saved, dict) or saved.keys() != required
            or type(saved["version"]) is not int or saved["version"] != 1):
        raise ValueError("已核验快照版本或字段不完整，请移除后重新核验")
    payload = {key: value for key, value in saved.items() if key != "identity_sha256"}
    if saved["identity_sha256"] != _snapshot_digest(payload):
        raise ValueError("已核验快照内容与身份校验不符，请移除后重新核验")
    fresh = _restoration_snapshot(restored)
    if saved["identity_sha256"] != fresh["identity_sha256"]:
        changed = [key for key in SNAPSHOT_FIELDS if _snapshot_digest(saved[key]) != _snapshot_digest(fresh[key])]
        raise ValueError("加入列表后原来源或参数已变化（" + "、".join(changed) + "）；请移除后重新核验，不能悄悄换参数")


def bind_fingerprint_match(match: dict, restored: dict) -> dict:
    """Pin a successfully verified source; never mutate or silently refresh an old pin."""
    from fifth_policy import require_selection_allowed
    require_selection_allowed(restored["selection"])
    fingerprint, directory = _match_record(match)
    if restored.get("fingerprint") != fingerprint or Path(restored.get("source_dir", "")).resolve() != directory:
        raise ValueError("已核验结果与所选指纹来源不一致，不能加入列表")
    if "verified_snapshot" in match:
        _check_verified_snapshot(match["verified_snapshot"], restored)
    result = copy.deepcopy(match)
    result.update(fingerprint=fingerprint, source_dir=str(directory),
                  verified_snapshot=_restoration_snapshot(restored))
    if restored.get("current_code_sha256") != restored["code_sha256"]:
        result["definition_status"] = "原定义可追溯"
    else:
        result.pop("definition_status", None)
    return result


def fingerprint_retirement_reason(match):
    """Recognize retired saved queue items without reading market data.

    Missing or opaque old records are still fully checked before binding/running;
    absence of a recognizable retired member is not execution authorization.
    """
    from fifth_policy import fifth_retirement_reason, require_selection_allowed

    snapshot = match.get("verified_snapshot") or {}
    selection = snapshot.get("selection") if isinstance(snapshot, dict) else None
    if isinstance(selection, dict):
        try:
            require_selection_allowed(selection)
        except ValueError as exc:
            if "永久删除" in str(exc):
                return str(exc)
    row = match.get("row") or {}
    if not isinstance(row, dict):
        return ""
    for timeframe, header in CASE_FIELDS:
        value = row.get(f"{timeframe}条件代码")
        if isinstance(value, int) or isinstance(value, str) and value.isdecimal():
            reason = fifth_retirement_reason(value)
            if reason:
                return f"{timeframe}：{reason}"
    for header in ("开仓规则", *(header for _, header in CASE_FIELDS)):
        for number in re.findall(r"(?<![A-Za-z0-9])F5-(\d{3,})(?!\d)", str(row.get(header, ""))):
            reason = fifth_retirement_reason(263 + int(number))
            if reason:
                return reason
    return ""


def _require_keys(value, keys, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是完整对象")
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(f"{label}缺少原始参数：{'、'.join(missing)}；不能用当前默认值补猜")


def _complete_selection(raw):
    keys = ("版本", "开仓指标", "开仓条件", *SINGLE_FIELDS, "成交偏移", "成本模式",
            "平仓后最小开仓间隔分钟", "手续费", "资金约束", "入场约束", "入场触发口径", "成交价格口径", "候选筛选")
    _require_keys(raw, keys, "原运行组合选择")
    _require_keys(raw["开仓条件"], [tf for tf, _ in CASE_FIELDS], "原开仓条件")
    _require_keys(raw["成交偏移"], ("开仓", "平仓"), "原成交偏移")
    _require_keys(raw["手续费"], ("开仓费率", "平仓费率", "BNB抵扣", "返佣比例"), "原手续费")
    _require_keys(raw["资金约束"], ("初始资金USDC", "最小开仓数量ETH", "最大开仓数量ETH",
                   "低于最小数量停止", "保护止损浮亏比例", "维持保证金率", "启用全仓强平", "资金费率"), "原资金约束")
    _require_keys(raw["入场约束"], ("最小S3距离", "S3基线周期"), "原入场约束")
    if type(raw["版本"]) is not int or raw["版本"] not in (19, 20):
        raise ValueError("仅支持能完整核验成本参数的原配置（配置版本19/20）；MACD_CYCLE沿用配置版本20")
    if "开仓位置过滤" not in raw and raw["版本"] >= 20:
        raise ValueError("该版本原配置缺少开仓位置过滤，无法准确恢复")
    try:
        normalized = 规范化配置(copy.deepcopy(raw))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"原运行组合选择包含不能准确恢复的参数：{exc}") from exc
    expected = copy.deepcopy(raw)
    expected["版本"] = normalized["版本"]
    # Only explicit, known UI labels may become their scalar code. The original
    # saved bytes still have to match the run identity below; unknowns are rejected.
    expected["入场触发口径"] = 规范化入场触发口径(expected["入场触发口径"])
    expected["成本模式"] = normalize_cost_mode(expected["成本模式"])
    if "开仓位置过滤" not in expected:
        expected["开仓位置过滤"] = ["OFF"]
    # v1.50 adds export-only controls. Missing keys in saved configurations mean
    # the original fixed-leverage stress screen, never the new default screen.
    if isinstance(expected["候选筛选"], dict):
        for key, value in (("筛选方案", "LEGACY_STRESS"), ("比较范围", "TARGET"), ("资金保留比例", .90)):
            expected["候选筛选"].setdefault(key, value)
    _unchanged_parameter(expected, normalized, "原运行组合选择")
    return normalized


def _combination_context(selection, directory=None, supplied=None):
    """Load only this run's verified definitions, then expose them to label helpers."""
    payload = supplied.get("指标组合字典") if isinstance(supplied, dict) else None
    if payload is None and directory is not None:
        path = Path(directory) / "指标组合字典.json"
        if path.is_file():
            payload = _read_json(path)
    if payload is None:
        if selection.get("指标组合"):
            raise ValueError("缺少原指标组合字典，无法准确还原成员及组合逻辑")
        return None
    registry = CombinationRegistry().update(payload)
    DEFAULT_REGISTRY.update(registry.to_dict())
    return registry


def _combination_names(names, registry):
    result = dict(names)
    if registry is not None:
        for identifier, _ in registry.iter_definitions("entry"):
            result[identifier] = combination_label("entry", identifier)
    return result


def _entry_name_codes(names):
    result = {}
    for code, name in names.items():
        result.setdefault(name, []).append(code)
    return result


def _narrow_strategy_dimensions(row, original, names, choices=None, name_codes=None):
    """Return atomic pools plus one exact group per selected compound dimension."""
    selection = dict(original)
    selection["开仓条件"] = dict(original["开仓条件"])
    selection.pop("指标组合", None)
    groups, cases = {}, []
    choices = choices if choices is not None else choices_for_config(original)
    name_codes = name_codes if name_codes is not None else _entry_name_codes(names)

    def narrow(kind, identifier, timeframe=None):
        definition = DEFAULT_REGISTRY.resolve(kind, identifier)
        if definition is None:
            return [identifier]
        spec = {"启用": True, "组合数量": [len(definition["members"])],
                "保留单项": False, "逻辑": definition["logic"]}
        if kind == "entry":
            groups.setdefault("开仓", {})[timeframe] = spec
        else:
            groups[{"stop": "止损", "tp": "止盈"}[kind]] = spec
        return list(definition["members"])

    for timeframe, header in CASE_FIELDS:
        candidates = [code for code in name_codes.get(row.get(header), ())
                      if code in choices["开仓"][timeframe]]
        if len(candidates) != 1:
            raise ValueError(f"无法唯一还原{timeframe}开仓条件：{row.get(header)!r}")
        code = candidates[0]
        cases.append(code)
        selection["开仓条件"][timeframe] = narrow("entry", code, timeframe)
    for target, header in SINGLE_FIELDS.items():
        value = row.get(header)
        kind = {"止损代码": "stop", "止盈方案编号": "tp"}.get(target)
        allowed = choices[{"stop": "止损", "tp": "止盈"}[kind]] if kind else original[target]
        if isinstance(value, bool) or value not in allowed:
            raise ValueError(f"原排行榜选项不属于原运行配置：{header}={value!r}")
        selection[target] = narrow(kind, value) if kind else [value]
    if groups:
        selection["指标组合"] = groups
    return selection, tuple(cases), row["止损代码"]


def _stop_index(code):
    if DEFAULT_REGISTRY.resolve("stop", code) is not None:
        from extended_rules import composite_stop_index
        return composite_stop_index(code)
    return next(index for index, item in enumerate(生成止损组合()) if item[0] == code)


def _unchanged_parameter(raw, normalized, label):
    """Normalization may reorder choices/coerce exact numbers, but never change meaning."""
    if isinstance(raw, dict) and isinstance(normalized, dict):
        if raw.keys() != normalized.keys():
            raise ValueError(f"{label}包含未知或不完整参数；不能丢弃或使用默认值补猜")
        for key in raw:
            _unchanged_parameter(raw[key], normalized[key], f"{label}.{key}")
        return
    if isinstance(raw, list) and isinstance(normalized, list):
        # All selection lists are scalar choice sets; their ordering does not affect trading.
        # Preserve typed membership (True is not 1) and exact int/float coercion,
        # without comparing every one of thousands of choices with every other.
        scalar_types = (str, int, float, bool, type(None))
        if all(type(value) in scalar_types for values in (raw, normalized) for value in values):
            def key(value):
                kind = type(value)
                return (int if kind is float and math.isfinite(value) else kind, value)
            choices = {key(value) for value in normalized
                       if not (type(value) is float and math.isnan(value))}
            if len(raw) == len(normalized) and all(
                    not (type(value) is float and math.isnan(value)) and key(value) in choices for value in raw):
                return
            raise ValueError(f"{label}包含未知值、非法类型或会被规范化改写的参数：{raw!r}")
        if (len(raw) == len(normalized)
                and all(any(type(item) is type(other) and item == other
                            or type(item) in (int, float) and type(other) in (int, float)
                            and math.isfinite(item) and item == other for other in normalized)
                        for item in raw)):
            return
    elif type(raw) in (int, float) and type(normalized) in (int, float):
        if math.isfinite(raw) and raw == normalized:
            return
    elif type(raw) is type(normalized) and raw == normalized:
        return
    raise ValueError(f"{label}包含未知值、非法类型或会被规范化改写的参数：{raw!r}")


def _same(actual, expected, label):
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        okay = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-15))
    else:
        okay = type(actual) is type(expected) and actual == expected
    if not okay:
        raise ValueError(f"排行榜与原运行配置不一致：{label}（{actual!r} / {expected!r}）")


def _datetime(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是原始时间字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}时间格式无效：{value}") from exc
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def _ranking_settings(directory):
    path = directory / "排行榜设置.json"
    if not path.is_file():
        return None
    settings = _read_json(path)
    _require_keys(settings, ("排行指标", "门槛"), "原排行榜设置")
    if settings["排行指标"] not in 排行指标选项 or not isinstance(settings["门槛"], list):
        raise ValueError("原排行榜设置含未知指标或无效门槛")
    result = {"排行指标": settings["排行指标"], "门槛": []}
    if "最优名额" in settings:
        result["最优名额"] = top_limit(settings)
    for item in settings["门槛"]:
        _require_keys(item, ("指标", "条件", "值"), "原排行门槛")
        if (item["指标"] not in 排行指标选项 or item["条件"] not in ("最低值", "最高值")
                or isinstance(item["值"], bool) or not isinstance(item["值"], (int, float))
                or not math.isfinite(item["值"])):
            raise ValueError("原排行榜门槛包含未知规则或无效数值")
        result["门槛"].append({key: item[key] for key in ("指标", "条件", "值")})
    return result


def restore_fingerprint_match(match: dict, *, _fresh=None, _hedge_context=None) -> dict:
    """Validate the selected source context and its data fingerprint, then restore one selection."""
    fingerprint, directory = _match_record(match)
    if match.get("kind") == "hedge":
        from hedge_fingerprints import restore_match
        return restore_match(match, _fresh=_fresh, _context=_hedge_context)
    fresh = find_fingerprint_matches(fingerprint, directory) if _fresh is None else _fresh.get(fingerprint, [])
    fresh = [item for item in fresh if Path(item["source_dir"]) == directory]
    if not fresh:
        raise ValueError("所选原排行榜已变化或该指纹已不存在，请重新查找")
    row = fresh[0]["row"]
    _same_original_row(match["row"], row)
    warnings = []
    configurations = []
    raw_sources = []
    selection_path = directory / "组合选择.json"
    if selection_path.is_file():
        raw = _read_json(selection_path)
        raw_sources.append(raw)
        configurations.append(_complete_selection(raw))
    checkpoint_path = directory / "断点记录.json"
    checkpoint = _read_json(checkpoint_path) if checkpoint_path.is_file() else None
    if checkpoint is not None:
        _require_keys(checkpoint, ("selection",), "原断点")
        raw_sources.append(checkpoint["selection"])
        configurations.append(_complete_selection(checkpoint["selection"]))
    if not configurations:
        raise ValueError("缺少原组合选择和断点selection，无法恢复未写入排行榜的S3及账户保护参数")
    if any(配置签名(value) != 配置签名(configurations[0]) for value in configurations[1:]):
        raise ValueError("原组合选择与断点selection的交易参数不一致，不能确定应恢复哪一份")
    selection = copy.deepcopy(configurations[-1])
    position = row.get("开仓位置过滤代码") or "OFF"
    if "开仓位置过滤代码" not in row and any(int(raw["版本"]) >= 20 for raw in raw_sources):
        raise ValueError("原排行榜缺少本版本应有的位置过滤字段")
    if position not in selection["开仓位置过滤"]:
        raise ValueError("排行榜位置过滤不属于原运行选择")
    names = dict(CODE_NAMES)
    dictionary = directory / "开仓扩展规则字典.json"
    if dictionary.is_file():
        rules = _read_json(dictionary)
        if not isinstance(rules, dict):
            raise ValueError("原开仓规则字典格式错误")
        for code, spec in rules.items():
            _require_keys(spec, ("名称",), "原开仓规则字典")
            names[int(code)] = spec["名称"]
    registry = _combination_context(selection, directory)
    names = _combination_names(names, registry)
    selection, cases, stop_code = _narrow_strategy_dimensions(
        row, selection, names, choices_for_config(selection, registry))
    fields = {"MACD柱": "hist", "DIF线": "dif"}
    field = fields.get(row.get("开仓MACD口径"))
    if field not in selection["开仓指标"]:
        raise ValueError("原排行榜MACD口径无效或不属于原运行选择")
    selection["开仓指标"] = [field]
    selection["开仓位置过滤"] = [position]
    entry_mode = row.get("入场触发口径")
    if not isinstance(entry_mode, str) or entry_mode not in 结果入场口径列表(selection, cases):
        raise ValueError("原排行榜入场触发口径不属于原运行选择，不能恢复整个模式列表")
    selection["入场触发口径"] = entry_mode
    cost_mode = row.get("成本模式")
    if not isinstance(cost_mode, str) or cost_mode not in cost_modes(selection):
        raise ValueError("原排行榜成本模式不属于原运行选择，不能恢复整个成本列表")
    selection["成本模式"] = cost_mode
    funds = selection["资金约束"]
    checks = {"初始资金（USDC）": funds["初始资金USDC"], "ETH最小开仓数量（ETH）": funds["最小开仓数量ETH"],
              "ETH单次最大开仓数量（ETH）": funds["最大开仓数量ETH"],
              "ETH最小开仓约束启用（0否1是）": int(funds["低于最小数量停止"]),
              "入场触发口径": selection["入场触发口径"], "成交价格口径": selection["成交价格口径"],
              "成本模式": selection["成本模式"], "平仓后最小开仓间隔（分钟）": selection["平仓后最小开仓间隔分钟"]}
    fees = selection["手续费"]
    checks.update(zip(("开仓基础手续费率（%）", "平仓基础手续费率（%）", "BNB手续费抵扣（0否1是）", "手续费返佣比例（%）"),
                      (fees["开仓费率"], fees["平仓费率"], int(fees["BNB抵扣"]), fees["返佣比例"])))
    checks.update(zip(("开仓净手续费率（%）", "平仓净手续费率（%）"), effective_fee_rates(selection)))
    checks.update(zip(("开仓成交偏移（%）", "平仓成交偏移（%）"), effective_slippage(selection)))
    for header, expected in checks.items():
        _same(row.get(header), expected, header)
    expected_id = stable_base_id(0 if field == "hist" else 1, cases, _stop_index(stop_code))
    if type(row.get("基础策略编号")) is not int or row["基础策略编号"] != expected_id:
        raise ValueError("基础策略编号与还原后的开仓/止损参数不符；不能使用Excel舍入后的编号")
    if (selection["候选筛选"]["启用"] and selection["候选筛选"].get("比较范围", "TARGET") == "TARGET"
            and selection["候选筛选"]["统一目标杠杆"] != selection["仓位倍数"][0]):
        selection["候选筛选"]["统一目标杠杆"] = selection["仓位倍数"][0]
        warnings.append("候选筛选的比较杠杆已匹配该结果唯一仓位，交易参数不变")
    selection = 规范化配置(selection)
    if 配置统计(selection)["包含仓位完整组合数"] != 1:
        raise ValueError("还原结果不是单一完整组合")

    meta = _read_json(directory / "回测数据说明.json")
    _require_keys(meta, ("request", "sources", "capabilities", "start_utc", "end_utc"), "原回测数据说明")
    _require_keys(meta["request"], ("start", "end", "fingerprint", "feature_version"), "原数据请求")
    _require_keys(meta["sources"], SOURCE_KEYS, "原数据来源")
    request = meta["request"]
    for key in ("start", "end"):
        if not isinstance(request[key], str):
            raise ValueError(f"原数据请求{key}必须是字符串")
        if request[key]:
            _datetime(request[key], f"原数据请求{key}")
    start_utc = _datetime(meta["start_utc"], "原实际开始时间")
    end_utc = _datetime(meta["end_utc"], "原实际结束时间")
    if end_utc <= start_utc:
        raise ValueError("原回测实际时间区间无效")
    capabilities = meta["capabilities"]
    if (not isinstance(capabilities, dict) or not all(type(v) is bool for v in capabilities.values())
            or not capabilities.get("ohlcv")):
        raise ValueError("原数据能力记录不完整或OHLCV不可用")
    timeframe_capabilities = meta.get("capabilities_by_timeframe", {})
    if (not isinstance(timeframe_capabilities, dict)
            or any(not isinstance(caps, dict) or not all(type(v) is bool for v in caps.values())
                   for caps in timeframe_capabilities.values())):
        raise ValueError("原数据各周期能力记录不完整")
    unavailable = [(tf, code) for tf, codes in selection["开仓条件"].items()
                   for code in unavailable_entry_codes(codes, capabilities_for_timeframe(
                       capabilities, timeframe_capabilities, tf), tf, allow_retired=True)]
    if unavailable:
        raise ValueError(f"原数据能力不足以运行该策略：{unavailable}")
    sources = {key: meta["sources"][key] for key in SOURCE_KEYS}
    if not sources["kline"]:
        raise ValueError("原数据说明缺少已解析的主K线来源")
    for key, path in sources.items():
        if not isinstance(path, str) or (path and not Path(path).is_file()):
            raise ValueError(f"原数据文件不存在或路径无效：{key}={path!r}")
    identity_path = directory / "回测运行身份.json"
    identity = _read_json(identity_path) if identity_path.is_file() else (checkpoint or {}).get("run_identity")
    if not isinstance(identity, dict):
        raise ValueError("缺少原回测运行身份，无法核对数据与计算版本")
    _require_keys(identity, ("feature_request", "engine_version", "code_sha256", "selection_signature"), "原回测运行身份")
    if identity["feature_request"] != request:
        raise ValueError("原运行身份与回测数据说明的请求不一致")
    if checkpoint and checkpoint.get("run_identity") is not None and checkpoint["run_identity"] != identity:
        raise ValueError("原断点与回测运行身份不一致")
    for raw in raw_sources:
        trade_parameters = copy.deepcopy(raw)
        trade_parameters.pop("候选筛选", None)
        encoded = json.dumps(trade_parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != identity["selection_signature"]:
            raise ValueError("原组合参数与运行身份的配置签名不符")
    if checkpoint and checkpoint.get("selection_signature") != identity["selection_signature"]:
        raise ValueError("原断点配置签名与运行身份不一致")
    engine_version = identity["engine_version"]
    if (not isinstance(engine_version, str) or not isinstance(identity["code_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["code_sha256"])):
        raise ValueError("原运行身份的计算版本或代码指纹无效")
    if row.get("回测计算版本") != engine_version + ":" + selection["成交价格口径"]:
        raise ValueError("原排行榜计算版本与运行身份不一致")
    expected_fingerprint = request["fingerprint"]
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
        raise ValueError("原数据指纹格式无效")
    if source_bundle_fingerprint(sources) != expected_fingerprint:
        raise ValueError("原数据文件指纹已变化（路径、修改时间或内容不一致），不能当作原结果数据恢复")
    if engine_version != ENGINE_VERSION:
        warnings.append(f"原计算版本为{engine_version}，当前为{ENGINE_VERSION}；已还原参数，计算结果可能不同")
    current_identity = build_run_identity(Path(__file__).resolve().parent, "", meta, ENGINE_VERSION)
    if identity["code_sha256"] != current_identity["code_sha256"]:
        warnings.append("原定义可追溯：当前核心计算代码指纹与原运行不同；保留原始参数及身份，重新运行按当前代码计算")
    restored = {"selection": selection, "sources": sources, "start": request["start"], "end": request["end"],
            "source_dir": str(directory), "fingerprint": fingerprint, "engine_version": engine_version,
            "current_engine_version": ENGINE_VERSION, "source_fingerprint": expected_fingerprint,
            "capabilities": dict(capabilities), "capabilities_by_timeframe": copy.deepcopy(timeframe_capabilities), "warnings": warnings, "ranking_settings": _ranking_settings(directory),
            "period": {"start_utc": meta["start_utc"], "end_utc": meta["end_utc"]},
            "request": dict(request), "code_sha256": identity["code_sha256"],
            "current_code_sha256": current_identity["code_sha256"], "origin_identity": copy.deepcopy(identity)}
    if "verified_snapshot" in match:
        _check_verified_snapshot(match["verified_snapshot"], restored)
    return restored
