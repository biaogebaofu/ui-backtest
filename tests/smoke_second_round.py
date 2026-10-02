"""小样本端到端验证：新止盈→账户重放→CSV/Excel→断点继续。只写独立验证目录。"""
import csv
import json
import subprocess
import sys
from itertools import islice
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from second_round import group_config, test_groups
from selection_config import 全选配置, 规范化配置
from strategy_space import 生成止盈方案


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python tests/smoke_second_round.py OUTPUT_DIR MARKET_CSV")
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(sys.argv[2]).resolve()
    sample = output / "验证K线.csv"
    with source.open(encoding="utf-8-sig", newline="") as src, sample.open("w", encoding="utf-8-sig", newline="") as dst:
        csv.writer(dst).writerows(islice(csv.reader(src), 12001))
    cfg = group_config(全选配置(), test_groups()[0])
    tps = 生成止盈方案()
    cfg["止盈方案编号"] = [1358, 8426] + [next(x.编号 for x in tps if x.类别 == cat) for cat in ("ATR倍数止盈", "缩量止盈")]
    cfg["开仓条件"]["1m"] = [1, 5]
    cfg["止损代码"] = ["S3_1m+S9_15m", "ATR50_1m", "BAD5_1m", "MA60C3_15m"]
    cfg["成交价格口径"] = "CLOSE_CONFIRMED"
    cfg["仓位倍数"] = [2., 5.]
    cfg["候选筛选"].update({"启用": True, "自动导出": True, "导出旧排行": True, "统一目标杠杆": 2.})
    settings = output / "验证组合.json"
    settings.write_text(json.dumps(规范化配置(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    result = output / "结果"
    cmd = [sys.executable, "-X", "utf8", str(PROJECT / "backtest_worker.py"), "--csv", str(sample),
           "--output", str(result), "--selection", str(settings), "--threads", "2", "--device", "cpu"]
    for attempt in range(2):
        proc = subprocess.run(cmd, cwd=PROJECT, encoding="utf-8", capture_output=True, check=True)
        (output / f"验证日志{attempt+1}.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        print(proc.stdout[-2500:], flush=True)
        current = (result / "全部回测结果.csv").read_bytes()
        if attempt == 0:
            original = current
        else:
            assert current == original, "断点继续改写了既有CSV"
            assert '"type": "tp_start"' not in proc.stdout, "断点继续重跑了策略"
    with (result / "全部回测结果.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 32
    assert {row["成交价格口径"] for row in rows} == {"CLOSE_CONFIRMED"}
    assert {int(row["止盈方案编号"]) for row in rows} == set(cfg["止盈方案编号"])
    from openpyxl import load_workbook
    for name in ("各类止盈最优前5000名.xlsx", "各类止盈最差1000名.xlsx", "分层候选分析_2x.xlsx"):
        book = load_workbook(result / name, read_only=True, data_only=True)
        assert book.sheetnames
        book.close()
    print("PASS: 4种止盈×2开仓×4止损×2杠杆；CSV、前5000/后1000、2x候选Excel可读；再次运行未重算。")


if __name__ == "__main__":
    main()
