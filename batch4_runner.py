"""Run exactly the declared fourth-batch configurations through the native UI worker."""
from pathlib import Path
import argparse,subprocess,sys,os,json,hashlib

def main():
    p=argparse.ArgumentParser(description='顺序运行第四批预声明任务；每次保留原生日志与断点')
    p.add_argument('--csv',required=True);p.add_argument('--output',required=True);p.add_argument('--threads',type=int,default=3);p.add_argument('--profile')
    a=p.parse_args();home=Path(__file__).resolve().parent;data=Path(a.csv).resolve();out=Path(a.output).resolve()
    if not data.is_file():p.error('CSV不存在')
    if a.threads<1:p.error('threads至少为1')
    names=sorted((home/'batch4_configs').glob('P*.json'))
    if a.profile:names=[x for x in names if x.stem==a.profile]
    if not names:p.error('找不到指定profile')
    out.mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256()
    with data.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    print('input sha256:',h.hexdigest(),flush=True)
    for config in names:
        dest=out/config.stem;dest.mkdir(exist_ok=True)
        command=[sys.executable,str(home/'backtest_worker.py'),'--csv',str(data),'--output',str(dest),'--selection',str(config),'--threads',str(a.threads),'--device','cpu','--cache-root',str(out/'feature_cache')]
        print('RUN',config.stem,flush=True)
        with (dest/'worker_console.log').open('a',encoding='utf-8') as log:
            rc=subprocess.run(command,cwd=home,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'OPENBLAS_NUM_THREADS':'1'}).returncode
        if rc:raise SystemExit(f'任务失败({rc})：{dest}/worker_console.log；修复原因再续跑，不混入不完整结果')
    print('请求的任务均完成；原生CSV在各P开头子目录',flush=True)
if __name__=='__main__':main()
