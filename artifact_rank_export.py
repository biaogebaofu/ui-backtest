"""Centered, filterable leaderboard exporter (optional artifact_tool backend)."""
from __future__ import annotations
import json
import math
import re
import unicodedata
from pathlib import Path
from rank_color_scales import add_artifact_scales
from strategy_description import validate_cell_lengths
from account_statistics import CAPACITY_DATE


def column_letter(index:int)->str:
    if index<1:raise ValueError(index)
    text=''
    while index:
        index,r=divmod(index-1,26);text=chr(65+r)+text
    return text


def clean(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,(dict,list,tuple)):return json.dumps(value,ensure_ascii=False)
    return value


def safe_name(name,used):
    base=re.sub(r'[\\/?*\[\]:]','_',str(name))[:31] or '工作表';name=base;n=1
    while name in used:n+=1;name=base[:27]+f'_{n}'
    used.add(name);return name


def workbook_steps(view):
    validate_cell_lengths(view)
    from artifact_tool import Workbook
    wb=Workbook.create()
    used={'回测设置','字段说明'}
    categories=view.get('分类',{})
    sheets={category:wb.worksheets.add(safe_name(category,used))
            for category in sorted(categories,key=lambda name:not name.startswith('全局'))}
    settings=wb.worksheets.add('回测设置')
    notes=[['项目','数值或说明','来源/适用范围']]
    notes.extend(view.get('排行范围说明',[]))
    meta=view.get('数据说明',{});plan=view.get('本次扫描说明',{})
    for key in ('数据范围','样本天数','输入数据','数据SHA256','回测引擎','扫描组合数','可用开仓条件数','扫描范围','扫描排除','排名口径','成交口径','交易成本','样本内说明','原界面门槛','原界面门槛通过数','理论与实盘差异','时间退出边界','已完成与计划行数一致'):
        if key in plan:notes.append([key,plan[key],'本次实际运行记录/配置'])
    for key,label in [('bars','1分钟K线根数'),('start_utc','开始时间UTC'),('end_utc','末根收盘时间UTC')]:
        if key in meta:notes.append([label,meta[key],'回测数据说明.json'])
    constant_rows={}
    for key,val in view.get('共同设置',[]):
        notes.append([key,val,'与主表一致，另集中列示']);constant_rows[key]=len(notes)
    if '初始资金（USDC）' not in constant_rows and view.get('初始资金') is not None:
        notes.append(['参考初始资金（USDC）',view['初始资金'],'混合资金时各行按本行参数计算，不用这个参考值'])
    notes.extend([
        ['完整原始数据','全部回测结果.csv','全部字段、全部已完成组合保留，未因排行榜筛选删行'],
        ['新增字段','排名、策略指纹、1m批次、数据依赖、开仓代码、中文止盈规则、账户净利润','仅展示层改动，未重新解释旧编号'],
        ['主表参数列','原始策略参数与共同设置保留在主表右侧','参数完整不等于全部算法已展开；未展开规则仍须核对原代码'],
        ['收益单位','CSV中的0.001=0.1%；Excel按百分比格式显示','未再乘100，避免收益被夸大100倍'],
        ['排名含义','按实际已完成扫描结果排序，可包含亏损；样本内，不构成实盘验证','不同退出参数是不同组合，不把行数叫作独立策略数量'],
    ])
    _write_table(settings,notes,'SettingsTable',header_height=30,body_height=38)
    settings.get_range('A:A').format.column_width=30
    settings.get_range('B:B').format.column_width=58
    settings.get_range('C:C').format.column_width=43
    for key,rownum in constant_rows.items():
        if '（%）' in key:settings.get_range(f'B{rownum}').set_number_format('0.00000%')
        elif 'USDC' in key:settings.get_range(f'B{rownum}').set_number_format('#,##0.00')
    for rownum,row in enumerate(notes[1:],2):
        lengths=[sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in str(v or '')) for v in row]
        lines=max(math.ceil(length/width) for length,width in zip(lengths,(28,54,39)))
        settings.get_range(f'A{rownum}:C{rownum}').format.row_height=max(38,min(120,lines*14+9))
    yield wb, '回测设置' 
    profit_header='账户净利润（USDC）';final_header='期末资金（USDC）'
    headers=view['表头']
    for i,(category,rows) in enumerate(view.get('分类',{}).items(),1):
        name=category;sheet=sheets[category]
        matrix=[headers]+[[str(x) if h=='基础策略编号' and x is not None else clean(x) for h,x in zip(headers,row)] for row in rows]
        _write_table(sheet,matrix,f'RankTable{i:02d}',header_height=40,body_height=44)
        for j,h in enumerate(headers,1):
            letter=column_letter(j)
            width=12
            if h=='排名':width=8
            elif h=='策略指纹':width=21
            elif h=='基础策略编号':width=24
            elif h=='入场次数规则':width=44
            elif h=='成本模式':width=18
            elif h in ('开仓规则','信号/ATR止损','止盈规则'):width=34 if h!='止盈规则' else 40
            elif h in ('固定止损','叠加止盈'):width=27
            elif h in ('数据依赖','5m过滤','4h过滤','1h过滤','15m过滤'):width=24
            elif 'JSON' in h:width=60
            elif '判据' in h or h in ('成交与成本说明','定义来源与版本核对','参数完整性'):width=55
            elif 'USDC' in h:width=18
            elif len(h)>12:width=19
            width={'策略指纹':22,'期末资金（USDC）':20,'最大回撤（%）':14,'名义倍数（倍）':14,
                   '胜率（%）':13,'日均完整交易（次/日）':19,'成本模式':16,CAPACITY_DATE:32,
                   '开仓数量上限（ETH）':18,'排名':9}.get(h,width)
            sheet.get_range(f'{letter}:{letter}').format.column_width=width
            if rows:
                rng=sheet.get_range(f'{letter}2:{letter}{len(rows)+1}')
                if h==CAPACITY_DATE:rng.set_number_format('yyyy-mm-dd')
                elif any(word in h for word in ('费率','偏移','返佣')):rng.set_number_format('0.000000%;[Red]-0.000000%;0.000000%')
                elif '（%）' in h:rng.set_number_format('0.00%;[Red]-0.00%;0.00%')
                elif 'USDC' in h:rng.set_number_format('#,##0.00;[Red]-#,##0.00;0.00')
                elif h in ('平均持仓（分钟）','日均完整交易（次/日）','利润因子PF'):rng.set_number_format('0.00')
                elif h=='策略指纹' or 'JSON' in h or h=='基础策略编号':rng.set_number_format('@')
                elif h in ('排名','开仓代码','交易次数（单）','爆仓保护（次）','全仓强平（次）','资金性停机（0否1是）','时间止损（分钟）'):rng.set_number_format('0')
        if rows and profit_header in headers and final_header in headers:
            pc=column_letter(headers.index(profit_header)+1);fc=column_letter(headers.index(final_header)+1)
            if '初始资金（USDC）' in headers:
                ic=column_letter(headers.index('初始资金（USDC）')+1);formula=f'={fc}2-{ic}2'
            elif '初始资金（USDC）' in constant_rows:
                formula=f"={fc}2-'回测设置'!$B${constant_rows['初始资金（USDC）']}"
            else:raise ValueError('缺少初始资金来源，不生成虚假净利润')
            sheet.get_range(f'{pc}2').formulas=[[formula]]
            if len(rows)>1:sheet.get_range(f'{pc}2:{pc}{len(rows)+1}').fill_down()
        if rows:
            add_artifact_scales(sheet,headers,2,len(rows))
            for h in ('累计收益率（%）','平均单笔净收益率（%）'):
                if h in headers:
                    c=column_letter(headers.index(h)+1)
                    sheet.get_range(f'{c}2:{c}{len(rows)+1}').conditional_formats.add_cell_is({'operator':'lessThan','formula':0,'format':{'font':{'color':'#9C0006'}}})
        yield wb, name
    dictionary=wb.worksheets.add('字段说明');used.add('字段说明')
    _write_table(dictionary,[['字段','解释']]+view.get('字段说明',[]),'DictionaryTable',header_height=30,body_height=50)
    dictionary.get_range('A:A').format.column_width=33;dictionary.get_range('B:B').format.column_width=72
    yield wb, '字段说明'
    if view.get('止盈方案字典'):
        tpdict=wb.worksheets.add('止盈方案字典')
        tpheads=['原编号','类别','周期','指标','参数一（原始小数）','参数二（原始小数）','参数三（原始小数）','完整说明及单位']
        _write_table(tpdict,[tpheads]+view['止盈方案字典'],'TakeProfitDictionary',header_height=40,body_height=72)
        tpdict.get_range('A:A').format.column_width=10
        tpdict.get_range('B:B').format.column_width=26
        tpdict.get_range('C:G').format.column_width=18
        tpdict.get_range('H:H').format.column_width=72
        yield wb, '止盈方案字典'



def build_workbook(view):
    wb = None
    for wb, _ in workbook_steps(view):
        pass
    return wb


def _write_table(sheet,matrix,name,header_height=34,body_height=32):
    rows=len(matrix);cols=len(matrix[0]);last=column_letter(cols)
    # Limit one RPC to 1000 rows. All formatting uses bounded ranges.
    for start in range(0,rows,1000):
        chunk=matrix[start:start+1000]
        sheet.get_range_by_indexes(start,0,len(chunk),cols).values=[[clean(v) for v in row] for row in chunk]
    sheet.get_range(f'A1:{last}{rows}').format={
        'font':{'name':'Calibri','size':10},'horizontal_alignment':'center','vertical_alignment':'center',
        'wrap_text':True,'row_height':body_height,'column_width':16,
    }
    if rows>1:sheet.tables.add(f'A1:{last}{rows}',True,name)
    sheet.get_range(f'A1:{last}1').format={
        'fill':'#17365D','font':{'bold':True,'color':'#FFFFFF','size':10},
        'horizontal_alignment':'center','vertical_alignment':'center','wrap_text':True,'row_height':header_height,
    }
    sheet.freeze_panes.freeze_rows(1)


def export_artifact_view(view,output_path):
    validate_cell_lengths(view)
    from artifact_tool import SpreadsheetFile
    wb=build_workbook(view)
    SpreadsheetFile.export_xlsx(wb).save(str(output_path))
    from xlsx_export_metadata import ensure_export_metadata
    ensure_export_metadata(output_path)
    return output_path
