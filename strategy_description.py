"""Export original parameters, with explicitly versioned current-code explanations."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path

from account_statistics import ENGINE_VERSION
from execution_settings import cost_modes, effective_fee_rates, effective_slippage
from fourth_batch import FOURTH_SPECS
from fifth_batch import FIFTH_SPECS
from extended_rules import stable_base_id
from run_safety import build_run_identity
from selection_config import 入场口径列表, 结果入场口径列表
from indicator_combinations import DEFAULT_REGISTRY, choices_for_config, combination_label, selection_pools
from strategy_space import 生成止损组合


PARAMETER_HEADERS = [
    '基础策略编号', '4小时条件', '1小时条件', '15分钟条件', '入场触发口径', '开仓方向', '交易会话',
    '止损代码', '固定止损代码', '叠加止盈代码', '叠加止盈说明', '开仓位置过滤代码',
    '止盈方案编号', '止盈类别', '止盈周期组合', '止盈指标', '止盈参数一', '止盈参数二', '止盈参数三',
    '止盈后等待分钟', '初始资金（USDC）', 'ETH最小开仓数量（ETH）', 'ETH单次最大开仓数量（ETH）',
    'ETH最小开仓约束启用（0否1是）', '成本模式', '成交价格口径', '平仓后最小开仓间隔（分钟）',
    '开仓成交偏移（%）', '平仓成交偏移（%）', '往返成交偏移（%）',
    '开仓基础手续费率（%）', '平仓基础手续费率（%）', 'BNB手续费抵扣（0否1是）', '手续费返佣比例（%）',
    '开仓净手续费率（%）', '平仓净手续费率（%）', '回测计算版本',
]
EXTRA_HEADERS = [
    '4h条件代码', '1h条件代码', '15m条件代码', '5m条件代码', '1m条件代码',
    '配置开仓偏移（%）', '配置平仓偏移（%）', '保护止损浮亏比例（%）', '维持保证金率（%）',
    '全仓强平启用', '固定资金费率（每8h）', '最小S3距离（%）', 'S3基线周期',
    '数据标的（元数据）', '数据源路径JSON', '请求开始时间', '请求结束时间', '实际开始UTC', '实际结束UTC',
    '数据指纹', '原核心代码SHA256', 'MACD计算定义', '开仓判据（当前代码解释）',
    '止损判据（当前代码解释）', '止盈判据（当前代码解释）', '成交与成本说明',
    '定义来源与版本核对', '参数完整性', '指标组合成员JSON', '完整单策略配置JSON',
]


def _read(path):
    return json.loads(path.read_text('utf-8-sig'))


def load_context(payload, source_dir, names):
    """Read once, without loading market data or guessing missing old parameters."""
    from fingerprint_lookup import _complete_selection, _combination_context, _combination_names, _entry_name_codes
    context = {'names': dict(names)}
    current = build_run_identity(Path(__file__).resolve().parent, '', {'request': {}}, ENGINE_VERSION)
    context['current_identity'] = current
    try:
        supplied = payload.get('运行上下文')
        if supplied is not None:
            if not isinstance(supplied, dict) or supplied.get('schema') != 1:
                raise ValueError('运行上下文版本无效')
            raw, data, identity = supplied['selection'], supplied['data'], supplied['identity']
        elif source_dir is not None:
            source_dir = Path(source_dir)
            checkpoint = _read(source_dir / '断点记录.json') if (source_dir / '断点记录.json').is_file() else {}
            raw = (_read(source_dir / '组合选择.json') if (source_dir / '组合选择.json').is_file()
                   else checkpoint.get('selection'))
            data = _read(source_dir / '回测数据说明.json')
            identity = (_read(source_dir / '回测运行身份.json') if (source_dir / '回测运行身份.json').is_file()
                        else checkpoint.get('run_identity'))
            if checkpoint.get('selection') is not None:
                # Both archives must identify the same trade configuration.
                first = copy.deepcopy(raw); first.pop('候选筛选', None)
                second = copy.deepcopy(checkpoint['selection']); second.pop('候选筛选', None)
                if first != second: raise ValueError('原组合选择与断点参数不一致')
        else:
            raise ValueError('缺少运行上下文及原结果目录')
        normalized = _complete_selection(raw)
        registry = _combination_context(normalized, source_dir, supplied)
        context['names'] = _combination_names(context['names'], registry)
        trade = copy.deepcopy(raw); trade.pop('候选筛选', None)
        signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
        if signature != identity['selection_signature'] or identity['feature_request'] != data['request']:
            raise ValueError('原参数/数据请求与运行身份不一致')
        for key in ('start_utc', 'end_utc', 'sources', 'request'):
            if key not in data: raise ValueError(f'原数据说明缺少{key}')
        for key in ('start', 'end', 'fingerprint'):
            if key not in data['request']: raise ValueError(f'原数据请求缺少{key}')
        if any(key not in data['sources'] for key in ('kline', 'micro', 'funding', 'oi', 'bundle')):
            raise ValueError('原数据来源不完整')
        if source_dir is not None and (Path(source_dir) / '开仓扩展规则字典.json').is_file():
            for code, spec in _read(Path(source_dir) / '开仓扩展规则字典.json').items():
                context['names'][int(code)] = spec['名称']
        runtime_context = {'schema': 1, 'selection': raw, 'data': data, 'identity': identity}
        if registry is not None:
            runtime_context['指标组合字典'] = registry.to_dict()
        context.update(selection=normalized, data=data, identity=identity, registry=registry,
                       choices=(choices_for_config(normalized, registry) if normalized.get('指标组合')
                                else selection_pools(normalized)), runtime_context=runtime_context,
                       stop_indices={code: index for index, (code, _, _) in enumerate(生成止损组合())},
                       name_codes=_entry_name_codes(context['names']))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        context['error'] = str(exc)
    return context


def _single_selection(row, context):
    from fingerprint_lookup import _same, _narrow_strategy_dimensions, _stop_index
    # Narrow the validated scan dimensions before copying; the original may list
    # thousands of TPs, while each exported row keeps exactly one of each choice.
    selection, cases, stop_code = _narrow_strategy_dimensions(
        row, context['selection'], context['names'], context.get('choices'), context.get('name_codes'))
    field = {'MACD柱': 'hist', 'DIF线': 'dif'}.get(row.get('开仓MACD口径'))
    if field not in selection['开仓指标']: raise ValueError('MACD口径与原配置不符')
    selection['开仓指标'] = [field]
    position = row.get('开仓位置过滤代码')
    if position not in selection['开仓位置过滤']: raise ValueError('位置过滤与原配置不符')
    selection['开仓位置过滤'] = [position]
    for key, options in (('入场触发口径', 结果入场口径列表(selection, cases)), ('成本模式', cost_modes(selection))):
        if not isinstance(row.get(key), str) or row[key] not in options: raise ValueError(f'{key}与原配置不符')
        selection[key] = row[key]
    funds, fees = selection['资金约束'], selection['手续费']
    checks = {'初始资金（USDC）': funds['初始资金USDC'], 'ETH最小开仓数量（ETH）': funds['最小开仓数量ETH'],
              'ETH单次最大开仓数量（ETH）': funds['最大开仓数量ETH'], 'ETH最小开仓约束启用（0否1是）': int(funds['低于最小数量停止']),
              '成交价格口径': selection['成交价格口径'], '平仓后最小开仓间隔（分钟）': selection['平仓后最小开仓间隔分钟'],
              '开仓基础手续费率（%）': fees['开仓费率'], '平仓基础手续费率（%）': fees['平仓费率'],
              'BNB手续费抵扣（0否1是）': int(fees['BNB抵扣']), '手续费返佣比例（%）': fees['返佣比例']}
    checks.update(zip(('开仓净手续费率（%）', '平仓净手续费率（%）'), effective_fee_rates(selection)))
    checks.update(zip(('开仓成交偏移（%）', '平仓成交偏移（%）'), effective_slippage(selection)))
    checks['回测计算版本'] = context['identity']['engine_version'] + ':' + selection['成交价格口径']
    for key, expected in checks.items(): _same(row.get(key), expected, key)
    stop_index = context['stop_indices'].get(stop_code)
    if stop_index is None:
        stop_index = _stop_index(stop_code)
    if (type(row.get('基础策略编号')) is not int
            or row['基础策略编号'] != stable_base_id(0 if field == 'hist' else 1, cases, stop_index)):
        raise ValueError('原基础策略编号不能准确核对')
    # load_context already normalized and checked the original configuration;
    # every replacement above must belong to that validated dimension.
    options = choices_for_config(selection, context.get('registry'))
    pools = [*options['开仓'].values(), options['止损'], options['止盈']]
    axes = ('开仓指标', '开仓方向', '交易会话', '开仓位置过滤', '固定止损代码',
            '强制时间止损分钟', '叠加止盈代码', '止盈后等待分钟', '仓位倍数')
    if (any(pool.count != 1 for pool in pools)
            or any(len(selection[key]) != 1 for key in axes)
            or len(入场口径列表(selection)) != 1 or len(cost_modes(selection)) != 1):
        raise ValueError('不是单一完整组合')
    return copy.deepcopy(selection)


def _entry_definition(code, field, mode):
    direction = f'方向取所选{field}相邻值的升/降，不是值的正负；相等延续前方向，前导无方向。'
    definition = DEFAULT_REGISTRY.resolve('entry', code)
    if definition is not None:
        return (combination_label('entry', code) + '；各成员在同一周期使用已收盘数据判断。成员判据：'
                + ' | '.join(_entry_definition(member, field, mode) for member in definition['members']))
    if code in FIFTH_SPECS:
        spec = FIFTH_SPECS[code]
        return (spec['name'] + '；' + spec['definition'] + '；第五轮独立方向I，按本方法已确认事件触发和重置，不附加隐含MACD方向或周期限次。'
                '高周期只在该原生K线收盘时发事件；显式选择的其他周期过滤、S3、位置、方向和会话仍需同时满足。'
                '本次成交按已选THEORETICAL/CLOSE_CONFIRMED原模型，不代表NEXT_OPEN或真实撮合。来源fifth_batch.py。')
    if code == 5:
        rule = '多：方向=+1且所选指标值>0；空：方向=-1且值<0。来源extended_signals.py/zero。'
    elif code in FOURTH_SPECS:
        spec = FOURTH_SPECS[code]
        rule = f"{spec['name']}；真实参数={list(spec['params'])}；{spec['definition']}；再与所选指标升/降方向及有限OHLCV校验相与。来源fourth_batch.py/fourth_masks。"
        if spec['family'] == 'hma':
            rule += 'WMA权重由旧至新1..n；n//2和sqrt(n)均向下取整；等号不满足价格/斜率严格比较。'
    elif code == 0:
        rule = '1m不启用：方向取已启用高周期；多空同时允许时不入场。来源backtest_engine.py/build_candidates。'
    else:
        rule = f'代码{code}的完整文字判据未展开；须结合原代码/版本核验，不能只凭名称重新实现。'
    modes = {'LIVE_01': '高周期持续状态；每个1m所选指标连续升/降段最多实际开仓一次。',
             'MACD_CYCLE': '1m hist=2×(DIF-DEA)过零换轮，零值延续、前导零不开；柱斜率反转不换轮，多空共用每轮一次名额。',
             'MACD_FULL_RED': '1m hist=2×(DIF-DEA)，正值红、负值绿，零值延续前色。固定红起点：从一次绿转红真实边界（含）到下一次绿转红边界（不含），红→绿→红为完整一轮；初始截断色段不开仓，先等真实绿转红边界。中间红转绿、柱斜率反转、止盈或止损平仓均不恢复次数；每轮多空共用一次实际开仓名额，未成交信号不消耗名额。新轮次仅恢复额度，仍须满足所选入场条件；不是每次换色重置。',
             'MACD_FULL_GREEN': '1m hist=2×(DIF-DEA)，正值红、负值绿，零值延续前色。固定绿起点：从一次红转绿真实边界（含）到下一次红转绿边界（不含），绿→红→绿为完整一轮；初始截断色段不开仓，先等真实红转绿边界。中间绿转红、柱斜率反转、止盈或止损平仓均不恢复次数；每轮多空共用一次实际开仓名额，未成交信号不消耗名额。新轮次仅恢复额度，仍须满足所选入场条件；不是每次换色重置。',
             'TF_EVENT': '1m扩展规则还需所选指标升/降方向发生翻转；1m未启用则最低启用高周期收盘为触发事件。',
             'F5_EVENT': '本项为第五轮事件的显式兼容过滤；开仓限次由第五轮事件编号管理，不使用旧MACD段/轮名额。'}
    return direction + rule + modes.get(mode, '未知入场模式，不能解释。') + '只用已收盘数据，公共前200根1m预热禁入；其他周期过滤、S3、位置、方向/会话仍需同时满足；具体代码见backtest_engine.py/build_candidates/build_field及run_all.py。'


def describe_row(row, context):
    result = {key: '未记录' for key in EXTRA_HEADERS}
    result['完整单策略配置JSON'] = ''
    result['指标组合成员JSON'] = ''
    current = context['current_identity']
    identity = context.get('identity', {})
    matched = (identity.get('code_sha256') == current['code_sha256'] and identity.get('engine_version') == ENGINE_VERSION)
    result['定义来源与版本核对'] = ('当前代码解释，非原运行定义快照；' + ('原/当前代码hash一致' if matched else '原/当前版本或代码不匹配，不能冒充原引擎定义')
                           + f"；原={identity.get('engine_version', '未记录')}；当前={ENGINE_VERSION}")
    result['止损判据（当前代码解释）'] = '按原止损代码、固定止损、强制时间与账户保护共同执行；具体分支见backtest_engine.py及account_replay.py，未展开的代码不能只凭名称重建。'
    atr = re.fullmatch(r'ATR(\d+)_(1m|5m|15m|1h|4h)', str(row.get('止损代码', '')))
    if atr:
        result['止损判据（当前代码解释）'] = (f'入场冻结距离={int(atr[1])/100:g}×最新已收{atr[2]} Wilder ATR14；'
            'TR=max(H-L,abs(H-前C),abs(L-前C))，首14根均值，后续(前ATR×13+TR)/14；多止损=C入场−距离、空=C入场+距离；'
            '从后续1m极值查首次触及；不是每根移动ATR止损。固定/时间及账户保护另计；见extended_stop_data/atr14。')
    result['止盈判据（当前代码解释）'] = (f"原记录：{row.get('止盈类别')} / {row.get('止盈指标')}，参数1/2/3="
        f"{row.get('止盈参数一')}/{row.get('止盈参数二')}/{row.get('止盈参数三')}；其他类别完整分支见backtest_engine.py/simple_tp_data/simulate_all_sizes。")
    if row.get('止盈类别') == '分批止盈':
        result['止盈判据（当前代码解释）'] += ('参数1为首段盈利门槛，参数2为首段平仓比例，不是回吐比例；'
            '参数3为余仓止盈方案编号，余仓须使用该编号的独立参数，不得套用本行参数1/2；余仓完整判据未展开，须查原止盈方案字典与代码。')
    elif row.get('止盈类别') == '移动止盈' and row.get('止盈指标') == '最高浮盈回吐比例':
        result['止盈判据（当前代码解释）'] += ('浮盈达到参数1后激活；后续K用此前最佳价，保留(1−参数2)的已获浮盈作为退出线；'
            '多=入场+(最佳高−入场)×(1−参数2)，空=入场−(入场−最佳低)×(1−参数2)；回吐比例不是价格跌幅，激活当根不按新线退出。')
    result['成交与成本说明'] = ('THEORETICAL按理论触碰位；CLOSE_CONFIRMED按触发K收盘确认，账户风险另按已有模型。'
        'SLIPPAGE仅扣有效开平偏移之和；FEE仅扣净手续费；两模式独立、不相加。配置偏移与有效偏移分列。'
        '完整算法见account_replay.py/backtest_engine.py，不模拟Maker排队；资金费率按已有每8h模型。')
    if context.get('error'):
        result['参数完整性'] = '不完整：' + context['error'] + '；未猜默认值，未生成可复制完整配置'
        return result
    try:
        selection = _single_selection(row, context)
        data, identity = context['data'], context['identity']
        choices = choices_for_config(selection)
        entry_codes = {tf: next(iter(codes)) for tf, codes in choices['开仓'].items()}
        for tf, code in entry_codes.items():
            result[f'{tf}条件代码'] = str(code) if abs(code) >= 10**15 else code
        groups = {'开仓': {tf: DEFAULT_REGISTRY.resolve('entry', code) for tf, code in entry_codes.items()
                          if DEFAULT_REGISTRY.resolve('entry', code) is not None}}
        for kind, label, key in (('stop', '止损', '止损代码'), ('tp', '止盈', '止盈方案编号')):
            definition = DEFAULT_REGISTRY.resolve(kind, row[key])
            if definition is not None:
                groups[label] = definition
                result[f'{label}判据（当前代码解释）'] = combination_label(kind, row[key])
        if groups['开仓'] or len(groups) > 1:
            result['指标组合成员JSON'] = json.dumps(groups, ensure_ascii=False, separators=(',', ':'))
        funds, entry = selection['资金约束'], selection['入场约束']
        result.update({'配置开仓偏移（%）': selection['成交偏移']['开仓'], '配置平仓偏移（%）': selection['成交偏移']['平仓'],
            '保护止损浮亏比例（%）': funds['保护止损浮亏比例'], '维持保证金率（%）': funds['维持保证金率'],
            '全仓强平启用': funds['启用全仓强平'], '固定资金费率（每8h）': funds['资金费率'],
            '最小S3距离（%）': entry['最小S3距离'], 'S3基线周期': entry['S3基线周期'],
            '数据标的（元数据）': data.get('symbol') or '未记录（不从文件名/ETH列名推断）',
            '数据源路径JSON': json.dumps(data['sources'], ensure_ascii=False),
            '请求开始时间': data['request']['start'], '请求结束时间': data['request']['end'],
            '实际开始UTC': data['start_utc'], '实际结束UTC': data['end_utc'],
            '数据指纹': data['request']['fingerprint'], '原核心代码SHA256': identity['code_sha256'],
            'MACD计算定义': data.get('macd', '原数据未记录MACD计算定义') + '；当前feature_builder.py：EMA首值=x首值，递推α=2/(n+1)；DIF=EMA12(C)−EMA26(C)，DEA=EMA9(DIF)，hist=2×(DIF−DEA)。',
            '开仓判据（当前代码解释）': '；'.join(
                f'{tf}：{_entry_definition(code, selection["开仓指标"][0], selection["入场触发口径"])}'
                for tf, code in entry_codes.items() if tf == '1m' or tf in groups['开仓'] or code in FIFTH_SPECS),
            '参数完整性': '已按原运行身份核对并收敛单策略；未重新校验当前市场文件内容；参数完整不等于全部算法已展开，不能只凭截图复写未展开规则',
            '完整单策略配置JSON': json.dumps({'schema': 1, 'selection': selection, 'sources': data['sources'],
                'start': data['request']['start'], 'end': data['request']['end'],
                'period': {'start_utc': data['start_utc'], 'end_utc': data['end_utc']},
                'request': data['request'], 'origin_identity': identity,
                'definitions_status': result['定义来源与版本核对']}, ensure_ascii=False, separators=(',', ':'), allow_nan=False)})
    except (ValueError, KeyError, TypeError) as exc:
        result['参数完整性'] = f'不完整或行参数冲突：{exc}；未生成可复制完整配置'
    return result


def validate_cell_lengths(view):
    for category, rows in view.get('分类', {}).items():
        for number, row in enumerate(rows, 2):
            for index, value in enumerate(row):
                if isinstance(value, str) and len(value.encode('utf-16-le')) // 2 > 32767:
                    raise ValueError(f'Excel单元格超过32767字符：{category} 第{number}行 {view["表头"][index]}；拒绝截断完整配置，请缩小附加说明或使用原始JSON')
