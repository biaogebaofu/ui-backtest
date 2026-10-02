"""Portable end-to-end pause/stop/resume proof using synthetic OHLCV.

Usage: python tests/verify_batch_resume.py <new-output-directory> [--csv real.csv]
This is a correctness test, NOT a strategy performance backtest.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from selection_config import 全选配置, 规范化配置

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output');parser.add_argument('--csv',default='')
    args=parser.parse_args()
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    if args.csv:
        source=Path(args.csv).resolve()
    else:
        import numpy as np
        import pandas as pd
        n=5760;x=np.arange(n);c=2000+5*np.sin(x/11)+2*np.sin(x/3);o=np.r_[c[0],c[:-1]]
        source=out/'ETHUSDC_测试数据_非行情.csv'
        pd.DataFrame({'openTime':1767355200000+x*60000,'open':o,'high':np.maximum(o,c)+.5,
                      'low':np.minimum(o,c)-.5,'close':c,'volume':10.+x%11}).to_csv(source,index=False)
    cfg=全选配置()
    cfg.update({'开仓指标':['hist'], '开仓条件':{'4h':[0], '1h':[0], '15m':[0], '5m':[0,5,6,7], '1m':list(range(1,18))},
                '止损代码':['S3_1m+S9_15m'],'固定止损代码':['OFF'],'叠加止盈代码':['OFF'],
                '强制时间止损分钟':[30],'止盈方案编号':[8866],'止盈后等待分钟':[0], '仓位倍数':[1.,2.]})
    cfg['入场约束']['最小S3距离']=0.
    cfg['候选筛选'].update({'启用':False,'自动导出':False,'导出旧排行':False})
    config=out/'验证组合.json';config.write_text(json.dumps(规范化配置(cfg),ensure_ascii=False),'utf8')
    env=os.environ.copy();env.pop('BT_FEATURES',None)
    def run(name,pause=False):
        target=out/name
        cmd=[sys.executable,'-X','utf8',str(ROOT/'backtest_worker.py'),'--csv',str(source),'--output',str(target),
             '--selection',str(config),'--threads',str(min(2,os.cpu_count() or 1)),'--device','cpu',
             '--cache-root',str(out/'shared_cache')]
        proc=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf8')
        paused=False; requested=False
        with (out/(name+('_pause' if pause else '_run')+'.log')).open('w',encoding='utf8') as log:
            try:
                for line in proc.stdout:
                    log.write(line);print(line.rstrip(),flush=True)
                    if pause and not requested and '生成开仓批次 1/' in line:
                        (target/'控制'/'暂停.flag').write_text('test','utf8');requested=True
                    if pause and '"type": "paused"' in line:
                        paused=True;(target/'控制'/'停止.flag').write_text('test','utf8')
                assert proc.wait(timeout=120)==0,'worker exited with an error'
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
        if pause:
            assert paused,'pause acknowledgement missing'
            cp=json.loads((target/'断点记录.json').read_text('utf8'))
            assert cp['next_tp']==0 and cp['entry_batch_next']>0,cp
            assert cp['rows']==32,cp['rows']
    run('暂停续跑',True);run('暂停续跑');run('连续运行')
    a=(out/'暂停续跑'/'全部回测结果.csv').read_bytes()
    b=(out/'连续运行'/'全部回测结果.csv').read_bytes()
    assert a==b,'暂停续跑与连续运行CSV不同'
    rows=list(csv.DictReader((out/'连续运行'/'全部回测结果.csv').open(encoding='utf-8-sig')))
    assert len(rows)==68,len(rows)
    (out/'验证结论.json').write_text(json.dumps({'rows':68,'batch_count':3,'paused_after_rows':32,
        'resumed_csv_byte_identical':True,'data_type':'provided' if args.csv else 'synthetic'},ensure_ascii=False,indent=2),'utf8')
    print('PASS：68组分3批，第1批后暂停并停止，续跑CSV与连续运行逐字节一致。')
if __name__=='__main__':main()
