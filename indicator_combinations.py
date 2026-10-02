"""Opt-in indicator combinations; legacy scalar choices retain their identifiers."""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import tempfile
from functools import lru_cache
from itertools import combinations
from pathlib import Path


TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")
REGISTRY_FILENAME = "指标组合字典.json"
KINDS = ("entry", "stop", "tp")


def canonical_members(members, kind):
    values = tuple(members)
    if kind not in KINDS:
        raise ValueError(f"未知指标组合类型：{kind}")
    if kind == "stop":
        if any(not isinstance(x, str) for x in values):
            raise ValueError("止损组合成员必须是规则代码")
        return tuple(sorted(set(values)))
    if any(isinstance(x, bool) or not isinstance(x, int) for x in values):
        raise ValueError("开仓/止盈组合成员必须是整数编号")
    return tuple(sorted(set(values)))


@lru_cache(maxsize=1)
def _tp_catalog():
    from strategy_space import 生成止盈方案
    return {row.编号: row for row in 生成止盈方案()}


@lru_cache(maxsize=1)
def _stop_catalog():
    from strategy_space import 生成止损组合
    return {row[0]: row for row in 生成止损组合()}


def combination_pool(kind, pool):
    """Return the canonical combinable pool, excluding OFF and staged exits."""
    values = canonical_members(pool, kind)
    if kind == "entry":
        return tuple(x for x in values if x != 0)
    if kind == "stop":
        return tuple(x for x in values if x != "OFF")
    catalog = _tp_catalog()
    return tuple(x for x in values if x != 1 and
                 (x not in catalog or catalog[x].类别 not in ("不使用止盈", "分批止盈")))


def _normalize_spec(raw, kind, pool=None):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("指标组合设置必须是对象")
    if not raw.get("启用", False):
        return None
    sizes = raw.get("组合数量", [2])
    if not isinstance(sizes, (list, tuple)) or not sizes:
        raise ValueError("启用指标组合后，至少选择一种组合数量")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 2 for x in sizes):
        raise ValueError("指标组合数量必须是大于等于2的整数")
    sizes = sorted(set(sizes))
    logic = raw.get("逻辑", "AND" if kind == "entry" else "OR")
    if logic not in ("AND", "OR"):
        raise ValueError("指标组合逻辑必须是AND或OR")
    expected_logic = "AND" if kind == "entry" else "OR"
    if logic != expected_logic:
        raise ValueError("开仓指标组合须同时满足（AND）；止损/止盈指标组合须任一满足（OR）")
    if pool is not None:
        available = len(combination_pool(kind, pool))
        if any(size > available for size in sizes):
            raise ValueError(f"指标组合最多可选{available}项；已选组合数量{sizes}超出有效指标池")
    return {"启用": True, "组合数量": sizes,
            "保留单项": bool(raw.get("保留单项", True)), "逻辑": logic}


def normalize_combinations(raw=None, pools=None):
    """Canonical active settings only; no active settings means legacy config."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("指标组合必须是对象")
    unknown = set(raw) - {"开仓", "止损", "止盈"}
    if unknown:
        raise ValueError(f"未知指标组合位置：{sorted(unknown)}")
    entries = raw.get("开仓", {})
    if not isinstance(entries, dict) or set(entries) - set(TIMEFRAMES):
        raise ValueError("开仓指标组合包含未知周期")
    result = {}
    for tf in TIMEFRAMES:
        pool = pools["开仓"][tf] if pools is not None else None
        spec = _normalize_spec(entries.get(tf), "entry", pool)
        if spec:
            result.setdefault("开仓", {})[tf] = spec
    for stage, kind in (("止损", "stop"), ("止盈", "tp")):
        pool = pools[stage] if pools is not None else None
        spec = _normalize_spec(raw.get(stage), kind, pool)
        if spec:
            result[stage] = spec
    return result


def _identifier(definition):
    payload = json.dumps(definition, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    if definition["kind"] == "stop":
        return f"@COMBO:{definition['logic']}:{digest.hex()[:32]}"
    return -(int.from_bytes(digest[:8], "big") & ((1 << 63) - 1) or 1)


class CombinationRegistry:
    """Only accessed combinations are registered, never the entire search space."""
    def __init__(self):
        self._definitions = {}

    def register(self, kind, members, logic="AND"):
        members = canonical_members(members, kind)
        if len(members) < 2 or logic not in ("AND", "OR"):
            raise ValueError("复合指标必须包含至少两个不同成员，并指定AND/OR")
        if combination_pool(kind, members) != members:
            raise ValueError("不启用或分批止盈选项不能加入指标组合")
        definition = {"kind": kind, "logic": logic, "members": list(members)}
        identifier = _identifier(definition)
        key = (kind, identifier)
        previous = self._definitions.get(key)
        if previous is not None and previous != definition:
            raise ValueError(f"指标组合编号冲突：{kind}/{identifier}")
        self._definitions[key] = definition
        return identifier

    def resolve(self, kind, identifier):
        definition = self._definitions.get((kind, identifier))
        return ({**definition, "members": list(definition["members"])}
                if definition is not None else None)

    def iter_definitions(self, kind=None):
        for (item_kind, identifier), definition in list(self._definitions.items()):
            if kind is None or kind == item_kind:
                yield identifier, {**definition, "members": list(definition["members"])}

    def to_dict(self):
        rows = [{"id": identifier, **definition}
                for identifier, definition in self.iter_definitions()]
        rows.sort(key=lambda row: (row["kind"], str(row["id"])))
        return {"版本": 1, "组合": rows}

    def update(self, payload):
        if not isinstance(payload, dict) or payload.get("版本") != 1 or not isinstance(payload.get("组合"), list):
            raise ValueError("指标组合字典格式或版本无效")
        staged = CombinationRegistry()
        for row in payload["组合"]:
            identifier = staged.register(row["kind"], row["members"], row["logic"])
            if identifier != row["id"]:
                raise ValueError("指标组合字典编号与成员不一致")
        for identifier, definition in staged.iter_definitions():
            previous = self.resolve(definition["kind"], identifier)
            if previous is not None and previous != definition:
                raise ValueError(f"指标组合编号冲突：{identifier}")
        self._definitions.update(staged._definitions)
        return self

    @staticmethod
    def _path(path):
        path = Path(path)
        return path if path.suffix.lower() == ".json" else path / REGISTRY_FILENAME

    def save(self, path):
        path = self._path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temp_path = stream.name
            json.dump(self.to_dict(), stream, ensure_ascii=False, indent=2)
        try:
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return path

    def load(self, path):
        path = self._path(path)
        if path.exists():
            self.update(json.loads(path.read_text(encoding="utf-8-sig")))
        return self


DEFAULT_REGISTRY = CombinationRegistry()


class CombinationChoices:
    """Indexable/reiterable sequence; `.count` stays exact above sys.maxsize."""
    def __init__(self, pool, spec=None, kind="entry", registry=None, off_values=None):
        self.kind = kind
        # Preserve historical pool order when disabled and for retained singles.
        self.pool = tuple(dict.fromkeys(pool))
        self.spec = _normalize_spec(spec, kind, self.pool)
        self.registry = registry if registry is not None else DEFAULT_REGISTRY
        self.members = combination_pool(kind, self.pool)
        if off_values is not None:
            self.members = tuple(x for x in self.members if x not in off_values)
        self._pool_set = frozenset(self.pool)
        self._member_set = frozenset(self.members)
        self.singles = self.pool if not self.spec or self.spec["保留单项"] else ()
        self.sizes = tuple(self.spec["组合数量"]) if self.spec else ()
        self.count = len(self.singles) + sum(math.comb(len(self.members), n) for n in self.sizes)

    def __len__(self):
        return self.count

    def __bool__(self):
        return self.count > 0

    def __iter__(self):
        yield from self.singles
        for size in self.sizes:
            for members in combinations(self.members, size):
                yield self.registry.register(self.kind, members, self.spec["逻辑"])

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.count)
            return [self[i] for i in range(start, stop, step)]
        index = operator.index(index)
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError("指标组合索引超出范围")
        if index < len(self.singles):
            return self.singles[index]
        index -= len(self.singles)
        total = len(self.members)
        for size in self.sizes:
            count = math.comb(total, size)
            if index >= count:
                index -= count
                continue
            chosen = []
            start = 0
            for remaining in range(size - 1, -1, -1):
                for candidate in range(start, total - remaining):
                    block = math.comb(total - candidate - 1, remaining)
                    if index < block:
                        chosen.append(self.members[candidate])
                        start = candidate + 1
                        break
                    index -= block
            return self.registry.register(self.kind, chosen, self.spec["逻辑"])
        raise IndexError("指标组合索引超出范围")

    def __contains__(self, identifier):
        if identifier in self._pool_set and identifier in self.singles:
            return True
        if not self.spec:
            return False
        definition = self.registry.resolve(self.kind, identifier)
        return bool(definition and definition["logic"] == self.spec["逻辑"] and
                    len(definition["members"]) in self.sizes and
                    set(definition["members"]).issubset(self._member_set))


def selection_pools(config):
    return {"开仓": config["开仓条件"], "止损": config["止损代码"],
            "止盈": config["止盈方案编号"]}


def choices_for_config(config, registry=None):
    pools = selection_pools(config)
    specs = normalize_combinations(config.get("指标组合"), pools)
    return {
        "开仓": {tf: CombinationChoices(pools["开仓"][tf], specs.get("开仓", {}).get(tf),
                                       "entry", registry) for tf in TIMEFRAMES},
        "止损": CombinationChoices(pools["止损"], specs.get("止损"), "stop", registry),
        "止盈": CombinationChoices(pools["止盈"], specs.get("止盈"), "tp", registry),
    }


def _definition(kind, identifier, registry=None):
    registry = registry if registry is not None else DEFAULT_REGISTRY
    definition = registry.resolve(kind, identifier)
    compound = (isinstance(identifier, int) and identifier < 0 or
                isinstance(identifier, str) and identifier.startswith("@COMBO:"))
    if compound and definition is None:
        raise ValueError(f"缺少指标组合字典：{kind}/{identifier}")
    return definition


def entry_members(code, registry=None):
    definition = _definition("entry", code, registry)
    return tuple(definition["members"]) if definition else (code,)


def entry_logic(code, registry=None):
    definition = _definition("entry", code, registry)
    return definition["logic"] if definition else "AND"


def stop_members(code, registry=None):
    definition = _definition("stop", code, registry)
    return tuple(definition["members"]) if definition else (code,)


def stop_logic(code, registry=None):
    definition = _definition("stop", code, registry)
    return definition["logic"] if definition else "OR"


def tp_members(code, registry=None):
    definition = _definition("tp", code, registry)
    return tuple(definition["members"]) if definition else (code,)


def tp_logic(code, registry=None):
    definition = _definition("tp", code, registry)
    return definition["logic"] if definition else "OR"


def combination_label(kind, identifier, registry=None):
    definition = _definition(kind, identifier, registry)
    if definition:
        labels = [combination_label(kind, member, registry) for member in definition["members"]]
        joiner = "且" if definition["logic"] == "AND" else "或"
        return f"指标组合（{joiner.join(labels)}）"
    if kind == "entry":
        from extended_rules import ENTRY_RULES
        legacy = {0: "不启用", 1: "情况一", 2: "情况二", 3: "情况三", 4: "情况四"}
        return ENTRY_RULES[identifier][0] if identifier in ENTRY_RULES else legacy.get(identifier, str(identifier))
    if kind == "stop":
        return _stop_catalog()[identifier][2]
    row = _tp_catalog()[identifier]
    return row.类别 + ("：" + row.说明 if row.说明 else "")


def expand_entry_options(config, timeframe, registry=None):
    spec = config.get("指标组合", {}).get("开仓", {}).get(timeframe)
    return CombinationChoices(config["开仓条件"][timeframe], spec, "entry", registry)


def expand_stop_specs(config, registry=None):
    options = CombinationChoices(config["止损代码"], config.get("指标组合", {}).get("止损"),
                                 "stop", registry)
    for code in options:
        yield stop_spec(code, registry)


def expand_tp_specs(config, registry=None):
    options = CombinationChoices(config["止盈方案编号"], config.get("指标组合", {}).get("止盈"),
                                 "tp", registry)
    for code in options:
        yield tp_spec(code, registry)


def stop_spec(code, registry=None):
    catalog = _stop_catalog()
    if code in catalog:
        return catalog[code]
    components = tuple(dict.fromkeys(part for member in stop_members(code, registry)
                                     for part in catalog[member][1]))
    return code, components, combination_label("stop", code, registry)


def tp_spec(code, registry=None):
    from strategy_space import 止盈方案
    catalog = _tp_catalog()
    return catalog[code] if code in catalog else 止盈方案(
        code, "指标组合止盈", 说明=combination_label("tp", code, registry))
