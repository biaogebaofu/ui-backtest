import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";
import { addRankColorScales } from "./rank_color_scales.mjs";
import { candidateDisplayPayload } from "./strategy_display.mjs";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error("用法：node candidate_excel_export.mjs 分层候选数据.json 输出.xlsx");
const data = candidateDisplayPayload(JSON.parse(await fs.readFile(inputPath, "utf8")));
for (const key of ["研究候选", "观察候选", "实盘候选"]) {
  (data[key] || []).forEach((row, index) => data.表头.forEach(header => {
    if (typeof row[header] === "string" && row[header].length > 32767)
      throw new Error(`Excel单元格超过32767字符：${key} 第${index + 5}行 ${header}；拒绝截断完整配置`);
  }));
}
const wb = Workbook.create();
const newScreen = data.设置.筛选方案 === "RETURN_DRAWDOWN";
const scopeLabel = data.设置.比较范围 === "ALL_RUN" ? "全部已回测倍数" : `指定已回测倍数 ${data.设置.统一目标杠杆}x`;
const selectionRule = newScreen
  ? `先过硬门槛 → 保留合格最高期末资金的${((data.设置.资金保留比例 ?? 0.9) * 100).toFixed(1)}%及以上 → 回撤升序（同回撤看资金/PF/倍数）`
  : "硬门槛 → 类内预筛分 → 相似簇去重 → 研究候选（原严格成本预筛）";

const COLORS = { navy: "#17365D", blue: "#D9EAF7", pale: "#F3F6FA", red: "#FCE4D6", green: "#E2F0D9", gold: "#FFF2CC", gray: "#D9E1F2" };
const colLetter = index => {
  let value = index + 1, text = "";
  while (value) { value--; text = String.fromCharCode(65 + value % 26) + text; value = Math.floor(value / 26); }
  return text;
};

function setTitle(sheet, title, subtitle, lastCol) {
  const titleLastCol = lastCol.length > 1 || lastCol > "P" ? "P" : lastCol;
  sheet.showGridLines = false;
  sheet.mergeCells(`A1:${titleLastCol}1`);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${titleLastCol}1`).format = { fill: COLORS.navy, font: { bold: true, color: "#FFFFFF", size: 16 }, rowHeight: 30, horizontalAlignment: "left" };
  sheet.mergeCells(`A2:${titleLastCol}2`);
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${titleLastCol}2`).format = { fill: COLORS.pale, font: { color: "#595959" }, wrapText: true, rowHeight: 34, horizontalAlignment: "left" };
}

function applyColumnFormats(sheet, headers, rowStart, rowEnd) {
  if (rowEnd < rowStart) return;
  headers.forEach((name, index) => {
    const letter = colLetter(index);
    const range = sheet.getRange(`${letter}${rowStart}:${letter}${rowEnd}`);
    if (["基础策略编号", "入场次数规则", "成本模式", "成本模式代码"].includes(name) || name.includes("JSON") || name.includes("指纹")) range.format.numberFormat = "@";
    else if (["费率", "偏移", "返佣"].some(word => name.includes(word))) range.format.numberFormat = "0.000000%;[Red](0.000000%);-";
    else if (name.includes("（%）") || name.includes("比例")) range.format.numberFormat = "0.0000%;[Red](0.0000%);-";
    else if (name.includes("USDC")) range.format.numberFormat = "0.000000;[Red](0.000000);-";
    else if (name.includes("倍）") || name.includes("t值")) range.format.numberFormat = "0.0000x;[Red](0.0000x);-";
    else if (name === "研究预筛分") range.format.numberFormat = "0.00";
    else if (name.includes("次数") || name.includes("编号") || name.includes("代码") || name.includes("分钟") || name.includes("区块")) range.format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
  });
}

function formulaColumn(sheet, headers, rows, name, formulaForRow) {
  const index = headers.indexOf(name);
  if (index < 0 || rows.length === 0) return;
  const letter = colLetter(index);
  sheet.getRange(`${letter}5:${letter}${4 + rows.length}`).formulas = rows.map((_, i) => [formulaForRow(5 + i)]);
}

let candidateTableSerial = 0;
function writeCandidateSheet(name, title, rows, emptyNote) {
  const sheet = wb.worksheets.add(name);
  const headers = data.表头;
  const lastCol = colLetter(headers.length - 1);
  setTitle(sheet, title, `${scopeLabel}｜${selectionRule}｜源文件：${data.源文件}`, lastCol);
  sheet.getRange(`A4:${lastCol}4`).values = [headers];
  sheet.getRange(`A4:${lastCol}4`).format = { fill: COLORS.navy, font: { bold: true, color: "#FFFFFF" }, wrapText: true, rowHeight: 52, horizontalAlignment: "center" };
  if (rows.length) {
    sheet.getRangeByIndexes(4, 0, rows.length, headers.length).values = rows.map(row => headers.map(header => header === "基础策略编号" && row[header] != null ? String(row[header]) : row[header] ?? null));
    if (headers.includes("完整单策略配置JSON")) sheet.getRangeByIndexes(4, 0, rows.length, headers.length).format = { wrapText: true, rowHeight: 44 };
    const idx = Object.fromEntries(headers.map((header, index) => [header, colLetter(index)]));
    // 原CSV的实际容量和已扣手续费净收益保持不动。压力情景仅替换往返偏移。
    formulaColumn(sheet, headers, rows, "p95成本后单笔收益（%）", r => `=${idx["平均成本后单笔收益（%）"]}${r}+${idx["平均往返偏移（%）"]}${r}-${idx["p95往返偏移（%）"]}${r}`);
    formulaColumn(sheet, headers, rows, "极端成本后单笔收益（%）", r => `=${idx["平均成本后单笔收益（%）"]}${r}+${idx["平均往返偏移（%）"]}${r}-${idx["极端往返偏移（%）"]}${r}`);
    formulaColumn(sheet, headers, rows, "扣手续费后收益÷平均往返偏移（倍）", r => `=IF(${idx["平均往返偏移（%）"]}${r}=0,"",${idx["扣手续费后、未扣成交偏移单笔收益（%）"]}${r}/${idx["平均往返偏移（%）"]}${r})`);
    applyColumnFormats(sheet, headers, 5, 4 + rows.length);
    addRankColorScales(sheet, headers, 5, rows.length);
    const table = sheet.tables.add(`A4:${lastCol}${4 + rows.length}`, true, `Candidates${++candidateTableSerial}Table`);
    table.style = "TableStyleMedium2";
    if (!newScreen) sheet.getRange(`${idx["研究预筛分"]}5:${idx["研究预筛分"]}${4 + rows.length}`).conditionalFormats.add("dataBar", { color: "#5B9BD5", gradient: true });
    sheet.getRange(`${idx["目标杠杆最大回撤（%）"]}5:${idx["目标杠杆最大回撤（%）"]}${4 + rows.length}`).conditionalFormats.add("colorScale", { colors: ["#E2F0D9", "#FFF2CC", "#F8CBAD"], thresholds: ["min", "50%", "max"] });
  } else {
    const noteLastCol = lastCol.length > 1 || lastCol > "P" ? "P" : lastCol;
    sheet.mergeCells(`A5:${noteLastCol}7`);
    sheet.getRange("A5").values = [[emptyNote]];
    sheet.getRange(`A5:${noteLastCol}7`).format = { fill: COLORS.gold, font: { bold: true, color: "#9C0006" }, wrapText: true, verticalAlignment: "center", horizontalAlignment: "left" };
  }
  headers.forEach((header, index) => {
    const width = header === "入场次数规则" ? 44 : header === "成本模式" ? 18 : header.includes("JSON") ? 60 : header.includes("判据") || ["成交与成本说明", "定义来源与版本核对", "参数完整性"].includes(header) ? 55 : header === "基础策略编号" ? 24 : header.includes("待验证") || header.includes("原因") ? 34 : header.includes("指纹") || header.includes("策略簇") ? 22 : Math.min(20, Math.max(10, header.length + 2));
    sheet.getRange(`${colLetter(index)}:${colLetter(index)}`).format.columnWidth = width;
  });
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(2);
  return sheet;
}

const guide = wb.worksheets.add("使用说明与门槛");
setTitle(guide, "本次回测候选分析（非实盘资格）", selectionRule, "H");
guide.getRange("A4:B4").values = [["项目", "结果/设置"]];
guide.getRange("A4:B4").format = { fill: COLORS.navy, font: { bold: true, color: "#FFFFFF" } };
const settingsRows = [
  ["完整CSV总行数", data.总行数], ["可计算硬门槛通过", data.初筛通过行数], ["每类研究候选", data.研究候选数],
  ["全局观察候选", data.观察候选数], ["全局实盘候选", data.实盘候选数], ["倍数比较范围", scopeLabel],
  ["最大回撤优选", data.设置.最大回撤优选], ["最大回撤硬上限", data.设置.最大回撤硬上限],
  ["利润因子PF下限", data.设置.盈亏比下限], ["持仓占时上限（新优选仅提示）", data.设置.容量占用率上限],
  ["多单占比范围", `${(data.设置.多单占比下限 * 100).toFixed(0)}%～${(data.设置.多单占比上限 * 100).toFixed(0)}%`],
  ["p95往返偏移（用户假设）", data.设置.p95往返偏移], ["极端往返偏移（用户假设）", data.设置.极端往返偏移],
  ["模型状态", data.实盘候选数 === 0 ? "研究候选可用；实盘候选未通过完整高级验证" : "已有严格实盘候选"],
  ["成交成本口径", data.成交成本说明 || "沿用源CSV执行假设；压力情景只替换成交偏移"],
  ["容量占用口径", data.容量口径说明 || "持仓时间加原止盈等待，不含新增全退出间隔，非盘口容量"],
  ...Object.entries(data.筛选阶段统计 || {}),
];
guide.getRangeByIndexes(4, 0, settingsRows.length, 2).values = settingsRows;
guide.getRange("B11:B12").format.numberFormat = "0.0%";
guide.getRange("B14").format.numberFormat = "0.0%";
guide.getRange("B16:B17").format.numberFormat = "0.000000%";
guide.getRange(`A${6 + settingsRows.length}:H${7 + settingsRows.length}`).merge();
guide.getRange(`A${6 + settingsRows.length}`).values = [["研究预筛分口径：" + data.研究预筛分说明]];
guide.getRange(`A${6 + settingsRows.length}:H${7 + settingsRows.length}`).format = { fill: COLORS.blue, wrapText: true, verticalAlignment: "center" };
let rowCursor = 9 + settingsRows.length;
guide.getRange(`A${rowCursor}:H${rowCursor}`).merge();
guide.getRange(`A${rowCursor}`).values = [["尚未计算、因此不能用于实盘资格的高级指标"]];
guide.getRange(`A${rowCursor}:H${rowCursor}`).format = { fill: COLORS.red, font: { bold: true, color: "#9C0006" } };
rowCursor++;
data.高级待验证项.forEach(item => { guide.getRange(`A${rowCursor}:H${rowCursor}`).merge(); guide.getRange(`A${rowCursor}`).values = [["• " + item]]; rowCursor++; });
rowCursor++;
guide.getRange(`A${rowCursor}:H${rowCursor}`).merge(); guide.getRange(`A${rowCursor}`).values = [["方法资料来源（用于审计口径）"]];
guide.getRange(`A${rowCursor}:H${rowCursor}`).format = { fill: COLORS.gray, font: { bold: true } }; rowCursor++;
(data.资料来源 || []).forEach(url => { guide.getRange(`A${rowCursor}:H${rowCursor}`).merge(); guide.getRange(`A${rowCursor}`).values = [[url]]; rowCursor++; });
guide.getRange("A:A").format.columnWidth = 32; guide.getRange("B:B").format.columnWidth = 42;
guide.getRange("C:H").format.columnWidth = 15;
guide.freezePanes.freezeRows(4);

writeCandidateSheet("每类研究候选", "每类止盈研究候选", data.研究候选, "没有策略通过当前可计算硬门槛；未为了凑数而降低门槛。");
writeCandidateSheet("全局观察候选", "全局观察候选", data.观察候选, "当前没有观察候选。");
writeCandidateSheet("实盘候选", "全局实盘候选", data.实盘候选,
  "严格结果：0条。当前汇总CSV缺少样本外逐笔/分窗矩阵，DSR、PBO、稳健t值、邻域稳定度等尚未验证，不能冒充实盘候选。");

const rejected = wb.worksheets.add("淘汰统计");
setTitle(rejected, "硬门槛淘汰统计", "同一行可能同时触发多个淘汰原因，因此原因计数之和可以大于源文件总行数。", "D");
rejected.getRange("A4:B4").values = [["淘汰原因", "涉及行数"]];
rejected.getRange("A4:B4").format = { fill: COLORS.navy, font: { bold: true, color: "#FFFFFF" } };
if (data.淘汰统计.length) {
  rejected.getRangeByIndexes(4, 0, data.淘汰统计.length, 2).values = data.淘汰统计.map(row => [row.淘汰原因, row.涉及行数]);
  const table = rejected.tables.add(`A4:B${4 + data.淘汰统计.length}`, true, "RejectionTable"); table.style = "TableStyleMedium2";
}
rejected.getRange("A:A").format.columnWidth = 48; rejected.getRange("B:B").format.columnWidth = 18;
rejected.freezePanes.freezeRows(4);

const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(outputPath);
