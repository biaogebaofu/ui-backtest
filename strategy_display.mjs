import fs from "node:fs/promises";

export const DISPLAY_LABELS = JSON.parse(await fs.readFile(new URL("./strategy_display.json", import.meta.url), "utf8"));

export function modeLabel(value, kind) {
  if (value == null || value === "") return "未记录（查原结果）";
  return DISPLAY_LABELS[kind][String(value)] ?? `未知模式：${value}（查原记录）`;
}

// Excel-only projection: do not write translated values into the source payload.
export function candidateDisplayPayload(payload) {
  const headers = payload.表头.filter(header => header !== "成本模式");
  const front = headers.includes("策略指纹") ? headers.indexOf("策略指纹") + 1 : 0;
  headers.splice(front, 0, "入场次数规则", "成本模式");
  if (payload.表头.includes("成本模式")) headers.push("成本模式代码");
  const result = { ...payload, 表头: headers };
  for (const key of ["研究候选", "观察候选", "实盘候选"]) {
    result[key] = (payload[key] || []).map(row => ({ ...row,
      入场次数规则: modeLabel(row.入场触发口径, "entry"),
      成本模式: modeLabel(row.成本模式, "cost"), 成本模式代码: row.成本模式 ?? null,
    }));
  }
  return result;
}
