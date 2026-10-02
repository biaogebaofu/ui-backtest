"""Human-readable leaderboard view. Does not alter native CSV or backtest logic.

Strategy settings remain on each row and are also collected on a settings sheet.
The original numeric percentage values (0.001 == 0.1%) are preserved.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
from extended_rules import ENTRY_RULES, ENTRY_RULE_BATCH, ENTRY_RULE_REQUIREMENTS, PLACEBO_CODES
from strategy_space import 生成止盈方案
from entry_position import position_filter_label
from strategy_description import PARAMETER_HEADERS, EXTRA_HEADERS, load_context, describe_row, validate_cell_lengths
from strategy_display import mode_label
from account_statistics import CAPACITY_TIMESTAMP, CAPACITY_DATE, capacity_date
from indicator_combinations import DEFAULT_REGISTRY, combination_label, tp_spec

SCHEMA_VERSION = 'v1.61-fifth-round-custom-rates'
CODE_NAMES = {0:'不启用',1:'情况一',2:'情况二',3:'情况三（反转量能充足）',4:'情况四（量能不足时反向）'}
CODE_NAMES.update({code: value[0] for code,value in ENTRY_RULES.items()})
NAME_CODES = {name:code for code,name in CODE_NAMES.items()}
TPS = {t.编号:t for t in 生成止盈方案()}
# Common settings are listed centrally and retained in the right-side parameter columns.
COMMON_FIELDS = [
 '4小时条件','1小时条件','15分钟条件','入场触发口径','开仓方向','交易会话',
 '叠加止盈说明','止盈后等待分钟','初始资金（USDC）',
 '开仓成交偏移（%）','平仓成交偏移（%）','往返成交偏移（%）',
 'ETH最小开仓数量（ETH）','ETH单次最大开仓数量（ETH）',
 'ETH最小开仓约束启用（0否1是）','回测计算版本','成交价格口径',
 '平仓后最小开仓间隔（分钟）','开仓基础手续费率（%）','平仓基础手续费率（%）',
 'BNB手续费抵扣（0否1是）','手续费返佣比例（%）','开仓净手续费率（%）','平仓净手续费率（%）',
 '成本模式',
]
# (source header, displayed header); descriptive values replace cryptic parameters.
VIEW_FIELDS = [
 ('策略指纹','策略指纹'),('开仓代码','开仓代码'),('1m批次','1m批次'),('数据依赖','数据依赖'),
 ('1分钟条件','开仓规则'),('开仓MACD口径','MACD口径'),
 ('入场次数规则','入场次数规则'),('成本模式中文','成本模式'),
 ('开仓位置过滤说明','开仓位置过滤'),
 ('4小时条件','4h过滤'),('1小时条件','1h过滤'),('15分钟条件','15m过滤'),('5分钟条件','5m过滤'),
 ('止损说明','信号/ATR止损'),('固定止损说明','固定止损'),('止盈规则','止盈规则'),
 ('叠加止盈说明','叠加止盈'),('强制时间止损（分钟）','时间止损（分钟）'),
 ('止盈后等待分钟','止盈后等待（分钟）'),('名义倍数（倍）','名义倍数（倍）'),
 ('初始资金（USDC）','初始资金（USDC）'),('交易次数（单）','交易次数（单）'),
 ('胜率（%）','胜率（%）'),('盈亏比（倍）','利润因子PF'),
 ('平均单笔收益率（%）','平均单笔净收益率（%）'),('平均持仓时间（分钟）','平均持仓（分钟）'),
 ('平均日完整交易数（次/日）','日均完整交易（次/日）'),('多单占比（%）','多单占比（%）'),
 ('期末资金（USDC）','期末资金（USDC）'),('账户净利润（USDC）','账户净利润（USDC）'),
 ('累计收益率（%）','累计收益率（%）'),('最大回撤（%）','最大回撤（%）'),
 ('爆仓保护次数（次）','爆仓保护（次）'),('全仓强平次数（次）','全仓强平（次）'),
 ('资金性停机标记（0否1是）','资金性停机（0否1是）'),
 ('实际容量占用率（%）','持仓及冷却占比（%）'),
]
FRONT_HEADERS = ['策略指纹', '期末资金（USDC）', '最大回撤（%）', '名义倍数（倍）',
                 '胜率（%）', '日均完整交易（次/日）', '成本模式']
VIEW_FIELDS.append((CAPACITY_DATE, CAPACITY_DATE))
VIEW_FIELDS.append(('开仓数量上限（ETH）', '开仓数量上限（ETH）'))
FIELD_NOTES = {
 '颜色说明':'期末资金和胜率由低到高为红、黄、绿；最大回撤由小到大为绿、黄、红。交易频率为蓝色、名义倍数为紫色深浅，不表示越高越好。仅手续费为浅蓝底，仅成交偏移为浅橙底。',
 CAPACITY_DATE:'首次实际开仓的计划数量达到或超过本行数量上限、按上限成交的北京时间日期（UTC+8）；逐仓位、逐成本独立记录。未达到和旧结果未记录分开显示，不能从期末资金倒推。',
 '排名':'仅在本工作表和本次已完成扫描范围内排序；不代表全参数空间的最优解或未来收益。',
 '策略指纹':'排行榜所列策略参数的SHA-256前16位，用于辨别组合、去重和查找原始记录；不是基础策略编号或可解码的参数串。未包含数据期间及部分运行设置，回填必须配合原结果目录的运行配置，不能仅凭指纹保证实盘效果或重跑结果一致。',
 '开仓代码':'原工具1分钟开仓条件代码，未重新编号。',
 '入场触发口径':'LIVE_01=所选hist/DIF连续升降方向段一次；MACD_CYCLE=旧口径，每次1m hist过零红绿换色后最多一次；MACD_FULL_RED=红→绿→红完整一轮一次，MACD_FULL_GREEN=绿→红→绿完整一轮一次，固定起点分别回测，中间换色和平仓不恢复名额，初始截断色段不开仓，先等真实起点边界；零值延续、多空共享每轮名额，不是DIF自身过零，柱仅变长短/实心空心不重置；TF_EVENT=最低启用周期收盘触发。各模式均可叠加位置过滤，不能混用结果。',
 '入场次数规则':'前部中文明确本行模式及完整周期起点，不改右侧机器代码；LIVE_01按所选1m指标连续升降段，MACD_CYCLE按1m hist每次过零换轮；MACD_FULL_RED/GREEN按对应固定颜色再次出现的真实边界换轮，中间换色和平仓不重置，多空共用一次实际开仓，未成交不消耗；零值延续、前导零及初始截断色段不开仓；TF_EVENT的扩展条件和过滤仍按原代码执行。',
 '1m批次':'1m条件来源：第一批v8、第二批0904、第三批扩展、第四批144—263；高周期过滤另列。',
 '数据依赖':'OHLCV/时间派生与成交微结构分开标记；未提供的资金费/OI不冒充已测试。',
 '开仓规则':'原工具1分钟条件名称；不另改交易判据。',
 '开仓位置过滤':'独立于原开仓规则的位置过滤；OFF=关闭位置过滤（原策略基线）。旧CSV缺少此字段时按OFF显示。完整代码和说明保留在原始数据中。',
 '利润因子PF':'原CSV“盈亏比（倍）”：正的单笔净收益率合计÷负的单笔净收益率绝对值合计。不是平均盈利/平均亏损，也不是预设止盈/止损距离比。',
 '平均单笔净收益率（%）':'原CSV“平均单笔收益率（%）”；新版按成本模式只扣成交偏移或只扣手续费，非两者叠加。旧结果保留原口径；返佣近似即时抵减。未模拟真实Maker排队、返佣延迟及资金费。不是账户复利收益率。',
 '成本模式':'SLIPPAGE=仅成交偏移，FEE=仅手续费。手续费模式不代表现实没有滑点；偏移模式不代表现实免手续费。旧数据没有此字段时须核对原费用及偏移列。',
 '期末资金（USDC）':'原引擎逐账户权益结果，包含数量上限、资金停机及保护退出的已有模型。',
 '账户净利润（USDC）':'Excel公式：期末资金－对应初始资金，不硬编码结果。',
 '累计收益率（%）':'原引擎逐账户复利累计收益率；CSV为小数，Excel显示为百分比。',
 '最大回撤（%）':'原工具逐K线权益回撤近似；不同成交口径及同根价格路径会影响结果。',
 '时间止损（分钟）':'0=关闭；>0=达到持仓分钟无论盈亏退出。独立时间止盈按各自说明仅在有毛浮盈时触发。',
 '资金性停机（0否1是）':'1表示权益或最小可下单数量限制使该账户后续不再成交。',
 '持仓及冷却占比（%）':'原CSV“实际容量占用率（%）”：（实际持仓分钟＋原止盈冷却分钟）/样本分钟，不含新增全平仓开仓间隔；不是盘口容量、市场冲击或成交容量。',
 '止盈规则':'主表保留原编号和简明判据；“止盈方案字典”保留完整说明与原始参数。移动止盈回吐针对已获浮盈，不是价格回撤。',
 '原始数据':'完整原生CSV始终保留；主表右侧保留原始策略参数和数量上限，未展示的逐年非复利收益合计、重复订单数仍可在原始CSV核对。',
 '完整单策略配置JSON':'从原运行存档校验后收敛至本行的单一开仓/止盈/仓位/入场模式/成本模式，selection为程序配置，其余字段保留数据期间与原身份；不是从16位指纹解码。当前数据文件内容未在导出时重新核验。',
 '定义来源与版本核对':'规则列为当前源码解释，不是原运行定义快照；原代码hash不匹配时不可冒充旧引擎精确定义。参数完整不等于全部算法已展开，不能只凭截图复写未展开规则。',
 '配置开仓偏移（%）':'原设置输入值；FEE下仍保留，但有效开仓成交偏移为0，不能重复扣除。费用/偏移数值均为比例，Excel以百分比显示。',
}


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


def ranking_scope_notes(payload):
    """One settings-page explanation shared by both presentation backends."""
    scope = payload.get('worst_scope')
    complete = scope == 'ALL_VALID_COMPLETED_V1'
    description = (payload.get('最差排行范围') or '全部数值有效的已完成账户结果，不应用最优优选门槛；包含资金停机和归零账户') if complete else '旧记录未声明全量范围，建议从完整CSV重建'
    notes = [
        ['本文件排行',payload.get('排行','最优'),'原排行榜JSON'],
        ['最差排行范围',description,f'worst_scope={scope}' if scope else '原记录无范围标记，不能确认全量最差'],
        ['最优门槛适用范围','最优组合门槛仅约束最优榜，不用于已声明全量范围的最差榜；旧榜须按范围标记核实','本版展示说明，不重新计算交易或改变指纹'],
    ]
    if '组合门槛' in payload:
        notes.append(['最优组合门槛',payload['组合门槛'],'仅对最优榜适用；不是全量最差榜的筛选条件'])
    return notes


def config_fingerprint(r:dict)->str:
    fields = ['止盈方案编号','止盈后等待分钟','基础策略编号','开仓MACD口径','4小时条件','1小时条件','15分钟条件','5分钟条件','1分钟条件','入场触发口径','止损代码','固定止损代码','叠加止盈代码','开仓方向','交易会话','强制时间止损（分钟）','名义倍数（倍）','初始资金（USDC）','ETH最小开仓数量（ETH）','ETH单次最大开仓数量（ETH）','开仓成交偏移（%）','平仓成交偏移（%）','回测计算版本','成交价格口径','ETH最小开仓约束启用（0否1是）']
    fields += ['平仓后最小开仓间隔（分钟）','开仓基础手续费率（%）','平仓基础手续费率（%）','BNB手续费抵扣（0否1是）','手续费返佣比例（%）','开仓净手续费率（%）','平仓净手续费率（%）']
    fields.append('成本模式')
    values = {k:r.get(k) for k in fields}
    values['开仓位置过滤代码'] = str(r.get('开仓位置过滤代码') or 'OFF').strip() or 'OFF'
    return hashlib.sha256(_canonical(values).encode()).hexdigest()[:16]


def _short_tp(tp):
    prefix=f'#{tp.编号} '
    if tp.类别=='固定比例止盈-周末相同':return prefix+f'固定{tp.参数一*100:g}%（周末同档）'
    if tp.类别=='固定比例止盈-周末独立':return prefix+f'固定：工作日{tp.参数一*100:g}% / 周末{tp.参数二*100:g}%'
    if tp.类别=='MACD反转止盈':return prefix+f'{tp.周期组合} {"MACD柱" if tp.指标=="hist" else "DIF"}连续{int(tp.参数一)}根反向，有浮盈'
    if tp.类别=='移动止盈' and tp.指标=='最高浮盈回吐比例':return prefix+f'启动{tp.参数一*100:g}%，回吐已获浮盈{tp.参数二*100:g}%'
    if tp.类别=='时间止盈':return prefix+f'{int(tp.参数一)}分钟当刻有毛浮盈才退出'
    if tp.类别=='ATR倍数止盈':return prefix+f'{tp.周期组合} ATR14×{tp.参数一:g}（入场冻结）'
    return prefix+(tp.说明 if len(tp.说明)<=48 else tp.说明[:44]+'…见字典')


def _enrich(r, context=None):
    result=dict(r)
    position_code = str(r.get('开仓位置过滤代码') or 'OFF').strip() or 'OFF'
    result['开仓位置过滤代码'] = position_code
    result['开仓位置过滤说明'] = position_filter_label(position_code)
    name = str(r.get('1分钟条件', ''))
    code = (next(iter(context['name_codes'].get(name, ())), None)
            if context and 'name_codes' in context else NAME_CODES.get(name))
    result['开仓代码']=(str(code) if abs(code) >= 10**15 else code) if code is not None else '未识别，查原名称'
    batch=ENTRY_RULE_BATCH.get(code,1 if code is not None and code in range(5) else None)
    result['1m批次']={1:'第一批·v8',2:'第二批·0904',3:'第三批',4:'第四批·重建',5:'第五轮·F5'}.get(batch,'未识别')
    requirements=ENTRY_RULE_REQUIREMENTS.get(code,frozenset({'ohlcv'}))
    entry_group = DEFAULT_REGISTRY.resolve('entry', code) if code is not None else None
    if entry_group is not None:
        result['1m批次'] = '指标组合'
        requirements = frozenset().union(*(ENTRY_RULE_REQUIREMENTS.get(member, frozenset({'ohlcv'}))
                                           for member in entry_group['members']))
    result['数据依赖']='资金费/OI' if any(x in requirements for x in ('funding','open_interest','open_interest_value')) else ('成交微结构' if requirements-{'ohlcv'} else 'OHLCV/时间')
    if code in PLACEBO_CODES:result['数据依赖']+='·安慰剂'
    tp_id = int(r.get('止盈方案编号', -1))
    tp = tp_spec(tp_id) if DEFAULT_REGISTRY.resolve('tp', tp_id) is not None else TPS.get(tp_id)
    result['止盈规则']=_short_tp(tp) if tp is not None else '未知编号，查原始字典'
    if r.get('止损代码') == 'OFF':result['止损说明']='OFF（固定/时间及账户保护另计）'
    elif DEFAULT_REGISTRY.resolve('stop', r.get('止损代码')) is not None:
        result['止损说明'] = combination_label('stop', r['止损代码'])
    if abs(tp_id) >= 10**15:
        result['止盈方案编号'] = str(tp_id)
    result['策略指纹']=config_fingerprint(r)
    result['入场次数规则']=mode_label(r.get('入场触发口径'),'entry')
    result['成本模式中文']=mode_label(r.get('成本模式'),'cost')
    if '基础策略编号' in r:result['基础策略编号']=str(r['基础策略编号']) # Excel numbers only retain 15 significant digits.
    result['账户净利润（USDC）']=None # filled with Excel formula
    result[CAPACITY_DATE]=capacity_date(r.get(CAPACITY_TIMESTAMP))
    result['开仓数量上限（ETH）']=r.get('ETH单次最大开仓数量（ETH）')
    return result


def build_view(payload:dict,source_dir:Path|None=None)->dict:
    if payload.get('导出视图版本')==SCHEMA_VERSION:return payload
    if payload.get('导出视图版本'):
        raise ValueError('请从原生排行榜JSON重新导出完整参数，旧阅读视图不能补猜已隐藏的字段')
    raw_headers=payload['表头'];categories=payload.get('分类',{})
    raw_records={name:[dict(zip(raw_headers,row)) for row in rows] for name,rows in categories.items()}
    flattened=[r for rows in raw_records.values() for r in rows]
    # No columns vanish solely because a single category has constant values.
    constant={k:flattened[0].get(k) for k in COMMON_FIELDS if flattened and all(_canonical(r.get(k))==_canonical(flattened[0].get(k)) for r in flattened)}
    fields=[(source,display) for source,display in VIEW_FIELDS if source not in constant]
    # Keep any normally common runtime field if an imported file actually mixes it.
    already={s for s,_ in fields}
    for k in COMMON_FIELDS:
        if k not in constant and k not in already and k in raw_headers:fields.append((k,'成本模式代码' if k=='成本模式' else k))
    # Keep machine codes on the right, separately from the leading Chinese labels.
    already={s for s,_ in fields}
    for key in PARAMETER_HEADERS + EXTRA_HEADERS:
        if key not in already:
            fields.append((key,'成本模式代码' if key=='成本模式' else key));already.add(key)
    context=load_context(payload,source_dir,CODE_NAMES)
    records={k:[{**_enrich(r,context),**describe_row(r,context)} for r in v] for k,v in raw_records.items()}
    is_worst=payload.get('排行')=='最差';limit=int(payload.get('名额',1000 if is_worst else 5000))
    records={name:rows[:limit] for name,rows in records.items()}
    # Category top-N union contains the true global top-N for this metric.
    metric=payload.get('排行指标','期末资金（USDC）');metric_col=metric.replace('越小越好','').strip();smaller='越小越好' in metric
    def key(r):
        value=r.get(metric_col)
        try:v=float(value)
        except (ValueError,TypeError):v=float('-inf')
        if not math.isfinite(v):v=float('-inf')
        return (-v if smaller else v,-float(r.get('最大回撤（%）',0)))
    unique={r['策略指纹']:r for rows in records.values() for r in rows}
    global_rows=sorted(unique.values(),key=key,reverse=not is_worst)[:limit]
    output_cats={('全局最差前' if is_worst else '全局前')+str(limit):global_rows,**records}
    view={**payload,'原始表头':raw_headers,'导出视图版本':SCHEMA_VERSION,'表头':['排名']+[d for _,d in fields],
          '分类':{name:[[i+1]+[r.get(src) for src,_ in fields] for i,r in enumerate(rows)] for name,rows in output_cats.items()},
          '共同设置':[[key,value] for key,value in constant.items()],
          '排行范围说明':ranking_scope_notes(payload),
          '字段说明':[[k,v] for k,v in FIELD_NOTES.items()],
          '原始数据保留':'全部回测结果.csv及原始排行JSON未改列名、未改值；本文件只是阅读视图。'}
    preferred = FRONT_HEADERS + [CAPACITY_DATE, '开仓数量上限（ETH）', '利润因子PF', '排名']
    order = [view['表头'].index(h) for h in preferred]
    order += [i for i in range(len(view['表头'])) if i not in order]
    view['表头'] = [view['表头'][i] for i in order]
    view['分类'] = {name: [[row[i] for i in order] for row in rows]
                    for name, rows in view['分类'].items()}
    used_tps = {int(r['止盈方案编号']) for r in flattened}
    plans = [tp_spec(i) if DEFAULT_REGISTRY.resolve('tp', i) is not None else TPS.get(i)
             for i in sorted(used_tps)]
    view['止盈方案字典'] = [[str(tp.编号) if abs(tp.编号) >= 10**15 else tp.编号,
                           tp.类别, tp.周期组合, tp.指标, tp.参数一, tp.参数二, tp.参数三, tp.说明]
                          for tp in plans if tp is not None]
    if source_dir:
        mp=source_dir/'回测数据说明.json'
        if mp.exists():view['数据说明']=json.loads(mp.read_text('utf-8-sig'))
        notes=source_dir/'本次扫描说明.json'
        if notes.exists():view['本次扫描说明']=json.loads(notes.read_text('utf-8-sig'))
    validate_cell_lengths(view)
    return view
