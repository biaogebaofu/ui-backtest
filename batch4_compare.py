from pathlib import Path
import csv,json,sys,hashlib,math
from decimal import Decimal
ROOT=Path(__file__).resolve().parent
PROJECT=ROOT
sys.path.insert(0,str(PROJECT))
from account_statistics import account_row
from strategy_space import 生成止盈方案,生成止损组合,生成固定止损档
TPS={x.编号:x for x in 生成止盈方案()};STOPS={x[0]:x[2] for x in 生成止损组合()};FSL={x[0]:x[3] for x in 生成固定止损档()}
DATA=Path('/mnt/data/ETHUSDC_klines_1m_2025-01-01_2026-09-05.csv')

def nominal(label):
 if '/' in label:
  a,b=label.split('/');return float(a)/float(b)
 return float(label.rstrip('x'))

def config_key(r,mode=True):
 # Canonicalise the proven alias ONLY for hist 1m; retain all execution dimensions.
 entry=5 if r['field']=='hist' and r['entry']==73 else r['entry']
 a=(entry,r['field'],r['c4h'],r['c1h'],r['c15m'],r['c5m'],r['entry_mode'],r['direction'],r['session'],r['stop'],r['fsl'],r['overlay'],r['tp'],r['hard'],r['wait'],r['size'],r['initial'],r['min_qty'],r['max_qty'],r['entry_cost'],r['exit_cost'],r['funding'])
 return a+(r['mode'],) if mode else a

def expand(path,source=None):
 out=[];p=Path(path)
 with p.open(encoding='utf-8-sig',newline='') as f:
  for row_i,row in enumerate(csv.DictReader(f)):
   labels=row['所选仓位顺序'].split(';')
   vk=['所选仓位期末资金（USDC）','所选仓位最大回撤（%）','所选仓位爆仓保护次数（次）','所选仓位全仓强平次数（次）','所选仓位资金性停机标记（0否1是）','所选仓位实际成交次数（单）']
   vs={k:row[k].split(';') for k in vk};assert all(len(v)==len(labels) for v in vs.values())
   for i,label in enumerate(labels):
    ar=account_row(row,i);tp=int(row['止盈方案编号'])
    r=dict(mode=row['成交价格口径'],entry=int(row['1分钟条件代码']),field='hist' if row['开仓MACD代码']=='0' else 'dif',stop=row['止损代码'],fsl=row['固定止损代码'],overlay=row['叠加止盈代码'],tp=tp,hard=int(row['强制时间止损（分钟）']),wait=int(row['止盈后等待分钟']),size=nominal(label),initial=float(row['初始资金（USDC）']),min_qty=float(row['ETH最小开仓数量（ETH）']),max_qty=float(row['ETH单次最大开仓数量（ETH）']),entry_cost=float(row['开仓成交偏移（%）']),exit_cost=float(row['平仓成交偏移（%）']),funding=0.,final=float(vs[vk[0]][i]),dd=float(vs[vk[1]][i]),protect=int(vs[vk[2]][i]),liq=int(vs[vk[3]][i]),halt=int(vs[vk[4]][i]),trades=int(ar['交易次数（单）']),win=float(ar['胜率（%）']),pf=float(ar['盈亏比（倍）']),avg=float(ar['平均单笔收益率（%）']),hold=float(ar['平均持仓时间（分钟）']),daily=float(ar['平均日完整交易数（次/日）']),long=float(ar['多单占比（%）']),occupancy=float(ar['实际容量占用率（%）']),source=source or p.parent.name,row=row_i+2)
    r.update(c4h=int(row['4小时条件代码']),c1h=int(row['1小时条件代码']),c15m=int(row['15分钟条件代码']),c5m=int(row['5分钟条件代码']),entry_mode=row['入场触发口径'],direction=row['开仓方向'],session=row['交易会话'])
    assert r['trades']==int(vs[vk[5]][i])
    r['id']=hashlib.sha256(json.dumps(config_key(r),ensure_ascii=False).encode()).hexdigest()[:16]
    out.append(r)
 return out

def dedup(rows):
 unique={};collisions=[]
 for r in rows:
  k=config_key(r)
  if k in unique:
   old=unique[k]
   for x in ['final','dd','trades','liq','halt']:
    if not math.isclose(old[x],r[x],rel_tol=1e-7,abs_tol=0.011 if x=='final' else 1e-9):
     collisions.append(dict(key=k,field=x,left=old[x],right=r[x],left_source=old['source'],right_source=r['source']))
  else:unique[k]=r
 if collisions:print('WARNING COLLISIONS',len(collisions),collisions[:3],flush=True)
 return list(unique.values()),collisions

def rank90(rows,limit=5000,fraction=.9):
 if len({r['mode'] for r in rows})>1: raise ValueError('不同成交口径不得混排')
 ordered=sorted([r for r in rows if r['trades']>0],key=lambda x:-x['final'])[:limit]
 if not ordered:return [],[],0.
 mx=ordered[0]['final']
 threshold=Decimal(str(mx))*Decimal(str(fraction))
 chosen=[dict(r,global_rank=i+1) for i,r in enumerate(ordered) if Decimal(str(r['final']))>threshold]
 chosen.sort(key=lambda x:x['dd'])  # stable: final-desc is tie-break inherited from full ranking
 return chosen,ordered,mx

def dump(path,obj):Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def write_csv(path,rows):
 if not rows: return
 keys=list(dict.fromkeys(k for r in rows for k in r))
 with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def read_expanded(path):
    integer={'entry','tp','hard','wait','protect','liq','halt','trades','row','c4h','c1h','c15m','c5m'}
    strings={'mode','field','stop','fsl','overlay','source','entry_mode','direction','session','id'}
    with Path(path).open(encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
    for r in rows:
        for k,v in r.items():
            if k in integer:r[k]=int(v)
            elif k not in strings:r[k]=float(v)
    return rows

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='仅合并同数据同成本的第四批原生结果；两模型分开严格90%筛选')
    p.add_argument('--previous',required=True);p.add_argument('--native-root',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    old=read_expanded(a.previous);new=[]
    for path in sorted(Path(a.native_root).glob('P*/全部回测结果.csv')):new+=expand(path,path.parent.name)
    if len(new)!=126720:raise ValueError(f'第四批未完成：应126720个账户，实际{len(new)}')
    for key,value in [('initial',1000),('min_qty',.01),('max_qty',20),('entry_cost',0.00005),('exit_cost',0.00005)]:
        if any(not math.isclose(r[key],value,rel_tol=1e-9) for r in old+new):raise ValueError(f'比较约束不同：{key}')
    merged,conflicts=dedup(old+new)
    if conflicts:raise ValueError('重复配置结果冲突，停止合并')
    write_csv(out/'merged_all_accounts.csv',merged);report={}
    for mode in ['THEORETICAL','CLOSE_CONFIRMED']:
        chosen,top,mx=rank90([r for r in merged if r['mode']==mode])
        write_csv(out/f'{mode}_top5000.csv',top);write_csv(out/f'{mode}_90_candidates.csv',chosen)
        write_csv(out/f'{mode}_top5.csv',chosen[:5]);report[mode]={'max':mx,'threshold':str(Decimal(str(mx))*Decimal('.9')),'qualified':len(chosen),'top5':chosen[:5]}
    dump(out/'summary.json',report);print(f'完成：{len(merged)}组，已分模型导出')
