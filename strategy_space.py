from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from itertools import combinations, product
from math import prod
from typing import Iterable
from extended_rules import ONE_MINUTE_CASE_CODES, entry_supported_timeframes, extra_stop_components


周期列表 = ("1m", "5m", "15m", "1h", "4h")
周期中文 = {"1m": "1分钟", "5m": "5分钟", "15m": "15分钟", "1h": "1小时", "4h": "4小时"}
仓位列表 = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
            2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 20.0, 50.0, 100.0)
# 固定比例退出档位。旧版 0.1%~1.0% 的代码保持不变，避免旧设置/断点失效；
# v1.41 只在旧空间外追加 0.01%~0.09% 的超短线细桶。
旧固定比例档 = tuple(i / 1000 for i in range(1, 11))
微固定比例档 = tuple(i / 10000 for i in range(1, 10))
固定止损比例档 = tuple(sorted(set((*微固定比例档, *旧固定比例档))))


def 比例显示(rate: float) -> str:
    return _decimal_text(_decimal_shift(Decimal(str(rate)), 2)) + "%"


def _decimal_shift(value: Decimal, places: int) -> Decimal:
    if not value.is_finite():
        raise ValueError("固定比例请输入有限数字")
    sign, digits, exponent = value.as_tuple()
    return Decimal((sign, digits, exponent + places))


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _valid_rate(value) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("固定比例请输入有效数字") from None
    if not rate.is_finite() or not 0 < rate < 1 or float(rate) == 0:
        raise ValueError("固定比例须大于 0% 且小于 100%；不使用时请选择 OFF")
    return rate


def 百分数转比例(text: str) -> Decimal:
    """输入 0.123456 或 0.123456% 都表示 0.123456%，不先四舍五入。"""
    value = str(text).strip().removesuffix("%").strip()
    try:
        return _valid_rate(_decimal_shift(Decimal(value), -2))
    except (InvalidOperation, ValueError):
        raise ValueError("请输入大于 0 且小于 100 的百分数，例如 0.123456 或 1.25%") from None


def 比例代码片段(rate: float) -> str:
    """旧档编号不变；自定义百分数直接编码，不依赖进程内注册表。"""
    rate = _valid_rate(rate)
    legacy = _decimal_shift(rate, 3)
    if legacy == int(legacy) and 1 <= legacy <= 10:
        legacy = int(legacy)
        return f"{legacy:02d}"
    basis_points = _decimal_shift(rate, 4)
    if basis_points == int(basis_points) and 1 <= basis_points <= 9:
        return f"B{int(basis_points):03d}"
    return "P" + _decimal_text(_decimal_shift(rate, 2)).replace(".", "p")


def 固定比例代码(prefix: str, weekday: float, weekend: float) -> str:
    if prefix not in ("FSL", "FTP"):
        raise ValueError("固定比例类型须为 FSL 或 FTP")
    return f"{prefix}{比例代码片段(weekday)}_{比例代码片段(weekend)}"


@lru_cache(maxsize=4096)
def 解析固定比例档(code: str, prefix: str) -> tuple[str, float, float, str]:
    """独立解码固定档；缓存只加速，不是代码身份或参数来源。"""
    if prefix not in ("FSL", "FTP"):
        raise ValueError("固定比例类型须为 FSL 或 FTP")
    if code == "OFF":
        return (code, 0.0, 0.0, "不使用固定比例止损" if prefix == "FSL" else "不叠加固定比例止盈")
    if not isinstance(code, str) or not code.startswith(prefix):
        raise ValueError(f"无效固定比例代码：{code}")
    try:
        parts = code[len(prefix):].split("_")
        if len(parts) != 2:
            raise ValueError
        rates = []
        for part in parts:
            if len(part) == 2 and part.isascii() and part.isdigit() and 1 <= int(part) <= 10:
                rate = Decimal(int(part)) / 1000
            elif len(part) == 4 and part.startswith("B") and part[1:].isascii() and part[1:].isdigit() and 1 <= int(part[1:]) <= 9:
                rate = Decimal(int(part[1:])) / 10000
            elif part.startswith("P"):
                rate = 百分数转比例(part[1:].replace("p", "."))
            else:
                raise ValueError
            rates.append(rate)
        wd, we = rates
        if 固定比例代码(prefix, wd, we) != code:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise ValueError(f"无效固定比例代码：{code}") from None
    label = (f"工作日与周末同为{比例显示(wd)}" if wd == we
             else f"工作日{比例显示(wd)}／周末{比例显示(we)}")
    return (code, float(wd), float(we), label)


def 补全固定比例档(codes, prefix: str) -> list[tuple[str, float, float, str]]:
    """保留预设顺序，将所选自定义档补入，稳定用于设置、指纹和子进程。"""
    selected = set(codes)
    parsed = {code: 解析固定比例档(code, prefix) for code in selected}
    presets = 生成固定止损档() if prefix == "FSL" else 生成叠加止盈档()
    rows = [row for row in presets if row[0] in selected]
    preset_codes = {row[0] for row in presets}
    rows.extend(parsed[code] for code in sorted(selected - preset_codes))
    return rows


def 固定比例说明(code: str, prefix: str) -> str:
    """导出历史记录时保留未知代码的身份，不能把未知档描述成未启用。"""
    try:
        return 解析固定比例档(code, prefix)[3]
    except ValueError:
        return f"未知固定比例（{code}）"


def 生成固定止损档() -> list[tuple[str, float, float, str]]:
    """返回 (代码, 工作日比例, 周末比例, 中文说明)。

    OFF 表示不使用固定比例止损。旧 0.1%~1.0% 的 FSL01..FSL10 编码完全保留；
    新增 0.01%~0.09% 细桶使用 Bxxx 编码。所有工作日/周末交叉都可测试。
    """
    rows = [("OFF", 0.0, 0.0, "不使用固定比例止损")]
    for weekday in 固定止损比例档:
        for weekend in 固定止损比例档:
            code = 固定比例代码("FSL", weekday, weekend)
            same = f"工作日与周末同为{比例显示(weekday)}"
            diff = f"工作日{比例显示(weekday)}／周末{比例显示(weekend)}"
            rows.append((code, weekday, weekend, same if abs(weekday - weekend) < 1e-12 else diff))
    return rows


# ---------------------------------------------------------------- 分析维度
# 下面三个维度默认都只勾中性值，不改变默认组合数；要做专项分析时再手动打开。
方向列表 = (
    ("BOTH", "多空都做"),
    ("LONG", "只做多"),
    ("SHORT", "只做空"),
)

# UTC 小时窗口。加密没有开收盘，但流动性有明显的地域节奏，
# 而且周末与工作日的点差和深度差别很大，值得单独拆开看。
会话列表 = (
    ("ALL", "全部时段"),
    ("WEEKDAY", "仅工作日（UTC 周一至周五）"),
    ("WEEKEND", "仅周末（UTC 周六、周日）"),
    ("ASIA", "亚洲时段 UTC 00:00–08:00"),
    ("EU", "欧洲时段 UTC 07:00–16:00"),
    ("US", "美洲时段 UTC 13:00–22:00"),
    ("EX_THIN", "排除清淡时段 UTC 22:00–02:00"),
)

# 时间止损：作为正式止损类别，不再放在“分析维度”里。
# 0=不启用；其余到点无论盈亏按收盘价退出。保留旧档并追加短/中/长持仓细桶。
强制时间止损档 = (0, 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1440)


def 生成叠加止盈档() -> list[tuple[str, float, float, str]]:
    """可以叠加在任意止盈方案之上的固定比例止盈；支持 0.01% 起的细桶。"""
    rows = [("OFF", 0.0, 0.0, "不叠加固定比例止盈")]
    for weekday in 固定止损比例档:
        for weekend in 固定止损比例档:
            code = 固定比例代码("FTP", weekday, weekend)
            same = f"工作日与周末同为{比例显示(weekday)}"
            diff = f"工作日{比例显示(weekday)}／周末{比例显示(weekend)}"
            rows.append((code, weekday, weekend, same if abs(weekday - weekend) < 1e-12 else diff))
    return rows


def 叠加止盈同档代码() -> list[str]:
    # 默认仍保持旧版 OFF + 0.1%~1.0% 共11档，避免默认组合数突然放大。
    return ["OFF"] + [固定比例代码("FTP", r, r) for r in 旧固定比例档]


def 叠加止盈全部同档代码() -> list[str]:
    return ["OFF"] + [固定比例代码("FTP", r, r) for r in 固定止损比例档]


def 会话说明(code: str) -> str:
    return dict(会话列表).get(code, code)


def 方向说明(code: str) -> str:
    return dict(方向列表).get(code, code)


def 固定止损同档代码() -> list[str]:
    """默认勾选集合仍是旧版 OFF + 0.1%~1.0% 十档，保持默认任务规模。"""
    return ["OFF"] + [固定比例代码("FSL", r, r) for r in 旧固定比例档]


def 固定止损全部同档代码() -> list[str]:
    return ["OFF"] + [固定比例代码("FSL", r, r) for r in 固定止损比例档]


@dataclass(frozen=True)
class 止盈方案:
    编号: int
    类别: str
    周期组合: str = ""
    指标: str = ""
    参数一: float = 0.0
    参数二: float = 0.0
    参数三: float = 0.0
    说明: str = ""

    def 字典(self) -> dict:
        return asdict(self)


def 全部非空周期组合() -> list[tuple[str, ...]]:
    return [c for n in range(1, len(周期列表) + 1) for c in combinations(周期列表, n)]


def 周期代码(items: Iterable[str]) -> str:
    return "+".join(items)


def 生成止损组合() -> list[tuple[str, tuple[str, ...], str]]:
    """151 种基础组合，再叠加 ⑨情况三止损 的 6 个状态（不用 + 5 个周期）。

    ⑨与 ①1分钟序列 一样是独立叠加项，可以搭在任意基础组合上，
    因此允许跨周期（例如 ③15分钟前轮极值 + ⑨5分钟情况三）。
    前 151 条保持原有顺序和索引不变，新增的追加在后面，
    这样旧结果里的「止损代码」仍然一一对应。
    """
    base: list[tuple[str, tuple[str, ...], str]] = [("S1", ("S1",), "①1分钟序列")]
    for tf in 周期列表:
        s3, s4, s5, s6 = f"S3_{tf}", f"S4_{tf}", f"S5_{tf}", f"S6_{tf}"
        cn = 周期中文[tf]
        labels = {
            s3: f"③{cn}前轮极值",
            s4: f"④{cn}反转量能不足",
            s5: f"⑤{cn}反转被反破",
            s6: f"⑥{cn}严格MACD反转",
        }
        components = (s3, s4, s5, s6)
        for count in range(1, len(components) + 1):
            for selected in combinations(components, count):
                label = "+".join(labels[item] for item in selected)
                base.append(("+".join(selected), selected, label))
                base.append(("S1+" + "+".join(selected), ("S1", *selected), "①1分钟序列+" + label))
    # 不使用任何信号止损：只靠⑧固定比例止损和②爆仓保护。
    # 这是判断"信号止损到底有没有贡献"的基准线，之前缺这一档。
    base.append(("OFF", (), "不使用信号止损（只靠⑧固定比例与②爆仓保护）"))
    result = list(base)
    for tf in 周期列表:
        s9 = f"S9_{tf}"
        label9 = f"⑨{周期中文[tf]}情况三反转量能充足"
        for code, components, label in base:
            result.append((f"{code}+{s9}", (*components, s9), f"{label}+{label9}"))
    # 前912项冻结。追加扩展部件单独使用及与任一原部件的二元组合。
    # 原有复合止损仍在前912项中；扩展规则不改写它们。
    old_atoms = [("S1", "①1分钟序列")]
    for tf in 周期列表:
        for code, label in (("S3", "前轮极值"), ("S4", "量能不足"), ("S5", "反破"), ("S6", "严格反转"), ("S9", "反转量能充足")):
            old_atoms.append((f"{code}_{tf}", f"{tf}{label}"))
    extra = extra_stop_components()
    for code, label in extra:
        result.append((code, (code,), label))
    for code, label in extra:
        for old, old_label in old_atoms:
            result.append((f"{old}+{code}", (old, code), f"{old_label}+{label}"))
    for (a, la), (b, lb) in combinations(extra, 2):
        result.append((f"{a}+{b}", (a, b), f"{la}+{lb}"))
    return result


def 生成入场组合():
    # 五个周期都有 0=不启用。1m 的 0 表示"不要求1分钟触发"，
    # 方向直接由高周期给出，用来单独衡量1分钟这层确认的贡献。
    choices = [tuple(c for c in ONE_MINUTE_CASE_CODES if tf in entry_supported_timeframes(c))
               for tf in ("4h", "1h", "15m", "5m", "1m")]
    for field in ("hist", "dif"):
        for c4h, c1h, c15m, c5m, c1m in product(*choices):
            yield field, (c4h, c1h, c15m, c5m, c1m)


def 生成止盈方案(包含无止盈: bool = True) -> list[止盈方案]:
    """生成Word中有限参数网格的全部方案。

    多周期规则：同一方案的指标和参数相同，只改变启用周期子集；五周期共有31个非空子集。
    这样完整覆盖1至5周期，同时避免“每周期使用完全不同参数”造成万亿级不可运行网格。
    """
    rows: list[止盈方案] = []

    def add(category, periods="", indicator="", p1=0.0, p2=0.0, p3=0.0, desc=""):
        rows.append(止盈方案(len(rows) + 1, category, periods, indicator,
                            float(p1), float(p2), float(p3), desc))

    if 包含无止盈:
        add("不使用止盈", desc="只使用所选止损，用作基准")

    fixed = [i / 1000 for i in range(1, 11)]
    for rate in fixed:
        add("固定比例止盈-周末相同", p1=rate,
            desc=f"工作日和周末均为{rate:.1%}")
    for weekday in fixed:
        for weekend in fixed:
            add("固定比例止盈-周末独立", p1=weekday, p2=weekend,
                desc=f"工作日{weekday:.1%}，周末{weekend:.1%}")

    subsets = 全部非空周期组合()
    for periods in subsets:
        code = 周期代码(periods)
        for indicator, label in (("hist", "MACD柱"), ("dif", "DIF线")):
            for confirm in (1, 2, 3):
                add("MACD反转止盈", code, indicator, confirm,
                    desc=f"{code}任一周期{label}连续{confirm}根已收盘柱反向且有浮盈"
                         f"（连续反向口径，不要求前一根同向，与止损⑥的严格反转不是同一判据）")

    for periods in subsets:
        code = 周期代码(periods)
        for ma in (20, 120):
            for confirm in (1, 2, 3):
                for activation in (0.002, 0.004, 0.006, 0.008, 0.010):
                    add("MA偏离回归止盈", code, f"MA{ma}", confirm, activation,
                        desc=f"{code}任一周期偏离MA{ma}至少{activation:.1%}后连续收窄{confirm}根且有浮盈")

    for periods in subsets:
        code = 周期代码(periods)
        for advance in (0.0, 0.0005, 0.001, 0.002):
            add("前高前低结构止盈", code, "MACD结构", advance,
                desc=f"{code}前方结构位，提前{advance:.2%}止盈")

    for r in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
        add("盈亏比止盈", indicator="R倍数", p1=r,
            desc=f"以入场时最近的③价格止损距离为1R，目标{r:g}R")

    # 移动止盈分两种口径，参数含义不混用：
    # 1) 回吐比例按“已获得的浮盈”计算；2) 固定回撤按最高/最低价格计算。
    activation_buckets = tuple(x / 1000 for x in range(1, 21))
    for activation in activation_buckets:
        for retrace in tuple(x / 100 for x in range(5, 51, 5)):
            add("移动止盈", indicator="最高浮盈回吐比例", p1=activation, p2=retrace,
                desc=f"浮盈{activation:.1%}启动，已获浮盈回吐{retrace:.0%}退出")

    for activation in activation_buckets:
        for retreat in tuple(x / 10000 for x in range(5, 51, 5)):
            add("移动止盈", indicator="最高价固定回撤", p1=activation, p2=retreat,
                desc=f"浮盈{activation:.1%}启动，价格自最高/最低点反向{retreat:.2%}退出")

    for activation in activation_buckets:
        for locked in (0.0, 0.0005, 0.001, 0.0015, 0.002):
            add("保本移动止盈", indicator="收盘启动次根生效", p1=activation, p2=locked,
                desc=f"收盘浮盈{activation:.1%}后，次根保本线锁定{locked:.2%}")

    for minutes in (15, 30, 60, 120, 240):
        add("时间止盈", indicator="持仓分钟", p1=minutes,
            desc=f"持仓{minutes}分钟时如有浮盈则收盘退出")

    for periods in subsets:
        code = 周期代码(periods)
        for indicator, label in (("hist", "MACD柱"), ("dif", "DIF线")):
            for lookback in (5, 10, 20):
                for confirm in (1, 2, 3):
                    add("经典背离止盈", code, indicator, lookback, confirm,
                        desc=f"{code}价格创新高/低但{label}未确认，窗口{lookback}根、连续确认{confirm}根")

    macd_remainders = [x for x in rows if x.类别 == "MACD反转止盈"]
    trailing_remainders = [x for x in rows if x.类别 == "移动止盈"]
    for first_rate in fixed:
        for remainder in (*macd_remainders, *trailing_remainders):
            add("分批止盈", remainder.周期组合, remainder.指标,
                first_rate, 0.5, remainder.编号,
                desc=f"盈利{first_rate:.1%}先平50%，余仓执行：{remainder.说明}")
    # 第二轮只追加，禁止插入旧网格：1358等历史编号及分批余仓引用保持不变。
    old = {(x.类别, x.指标, x.参数一, x.参数二) for x in rows}
    for activation in (0.0005, 0.00075, 0.001, 0.00125, 0.0015, 0.00175, 0.002, 0.0025):
        for retrace in (0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.125, 0.15, 0.20):
            if ("移动止盈", "最高浮盈回吐比例", activation, retrace) not in old:
                add("移动止盈", indicator="最高浮盈回吐比例", p1=activation, p2=retrace,
                    desc=f"第二轮细桶：浮盈{activation:.3%}启动，已获浮盈回吐{retrace:.1%}退出")
    for tf in 周期列表:
        for ma in (20, 120):
            for confirm in (1, 2, 3):
                add("均线穿越止盈", tf, f"MA{ma}", confirm,
                    desc=f"{tf}收盘从MA{ma}有利侧穿到不利侧，连续确认{confirm}根且有毛浮盈时退出")
        for ratio in (0.3, 0.5, 0.7):
            for confirm in (1, 2, 3):
                add("缩量止盈", tf, "前20根均量", ratio, confirm,
                    desc=f"{tf}成交量≤前20根均量的{ratio:.0%}，连续{confirm}根且有毛浮盈时退出")
    for minutes in (1, 3, 5, 10, 45, 90):
        add("时间止盈", indicator="持仓分钟", p1=minutes,
            desc=f"第二轮细桶：持仓{minutes}分钟当刻如有毛浮盈则收盘退出；当刻亏损不补触发")
    # 回吐系数0.01—0.1，每0.005一档；扩展全部现有启动值，不改变1358/1359。
    old = {(x.类别, x.指标, x.参数一, x.参数二) for x in rows}
    activations = sorted({x.参数一 for x in rows if x.类别 == "移动止盈" and x.指标 == "最高浮盈回吐比例"})
    for activation in activations:
        for bucket in range(2, 21):
            retrace = bucket / 200.
            if ("移动止盈", "最高浮盈回吐比例", activation, retrace) not in old:
                add("移动止盈", indicator="最高浮盈回吐比例", p1=activation, p2=retrace,
                    desc=f"自由细桶：浮盈{activation:.3%}启动，已获浮盈回吐{retrace:.1%}退出（系数{retrace:g}）")
    for tf in 周期列表:
        for k in (.5, 1., 1.5, 2., 3., 4.):
            add("ATR倍数止盈", tf, "ATR14开仓冻结", k,
                desc=f"{tf}入场时已收盘ATR14的{k:g}倍作为价格目标，持仓不重算ATR")

    # v1.41 追加式扩展：不能插进前面的旧网格，否则 1358 等历史编号会整体漂移。
    # 固定比例止盈新增 0.01%~0.09%；周末独立矩阵补齐“至少一侧为细桶”的组合。
    for rate in 微固定比例档:
        add("固定比例止盈-周末相同", p1=rate,
            desc=f"v1.41细桶：工作日和周末均为{比例显示(rate)}")
    for weekday in 固定止损比例档:
        for weekend in 固定止损比例档:
            if weekday in 微固定比例档 or weekend in 微固定比例档:
                add("固定比例止盈-周末独立", p1=weekday, p2=weekend,
                    desc=f"v1.41细桶：工作日{比例显示(weekday)}，周末{比例显示(weekend)}")

    # 时间止盈继续是“到点且有浮盈才走”，只追加旧列表缺失的持仓时长。
    for minutes in (2, 20, 180, 360, 480, 720, 1440):
        add("时间止盈", indicator="持仓分钟", p1=minutes,
            desc=f"v1.41持仓时间细桶：{minutes}分钟时如有毛浮盈则收盘退出")
    return rows


def 统计组合数() -> dict[str, int]:
    tps = 生成止盈方案()
    by_category: dict[str, int] = {}
    for tp in tps:
        by_category[tp.类别] = by_category.get(tp.类别, 0) + 1
    entry_count = 2 * prod(sum(tf in entry_supported_timeframes(c) for c in ONE_MINUTE_CASE_CODES)
                           for tf in 周期列表)
    stop_count = len(生成止损组合())
    fixed_stop_count = len(生成固定止损档())
    _ = (len(方向列表), len(会话列表), len(强制时间止损档))
    base_count = entry_count * stop_count
    return {
        "入场组合数": entry_count,
        "止损组合数": stop_count,
        "固定止损档数": fixed_stop_count,
        "基础策略数": base_count,
        "止盈方案数": len(tps),
        "仓位档数": len(仓位列表),
        "不含仓位完整组合数": base_count * len(tps),
        "包含仓位完整组合数": base_count * len(tps) * len(仓位列表),
        **{f"止盈_{k}": v for k, v in by_category.items()},
    }


if __name__ == "__main__":
    import json
    print(json.dumps(统计组合数(), ensure_ascii=False, indent=2))
