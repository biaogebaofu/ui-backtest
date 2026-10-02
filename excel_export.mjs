import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";
import { addRankColorScales } from "./rank_color_scales.mjs";

const inputPath = process.argv[2];
const outputPath = process.argv[3];
if (!inputPath || !outputPath) throw new Error("用法：node excel_export.mjs 排行.json 输出.xlsx");

const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const headers = payload["表头"];
const categories = payload["分类"];
const ranking = payload["排行"] || "最优";
const limit = Number(payload["名额"] || 5000);
const workbook = Workbook.create();
const navy = "#1F4E78";
const blue = "#D9EAF7";
const note = "#FFF2CC";
const green = "#E2F0D9";
const border = "#D9E2F3";

function safeSheetName(name, used) {
  let value = name.replace(/[\\/?*\[\]:]/g, "_").slice(0, 31);
  let i = 2;
  while (used.has(value)) value = `${name.slice(0, 27)}_${i++}`.slice(0, 31);
  used.add(value);
  return value;
}

function columnLetter(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function findHeader(...names) {
  for (const name of names) {
    const index = headers.indexOf(name);
    if (index >= 0) return index;
  }
  return -1;
}

function styleTitle(sheet, title, noteText, cols) {
  sheet.showGridLines = false;
  sheet.getRangeByIndexes(0, 0, 1, cols).merge();
  sheet.getCell(0, 0).values = [[title]];
  sheet.getRangeByIndexes(0, 0, 1, cols).format = {
    fill: navy, font: { bold: true, color: "#FFFFFF", size: 15 }, verticalAlignment: "center",
  };
  sheet.getRangeByIndexes(1, 0, 1, cols).merge();
  sheet.getCell(1, 0).values = [[noteText]];
  sheet.getRangeByIndexes(1, 0, 1, cols).format = { fill: note, font: { color: "#C00000", bold: true }, wrapText: true };
  sheet.getRangeByIndexes(3, 0, 1, cols).format = { fill: navy, font: { bold: true, color: "#FFFFFF" }, wrapText: true };
  sheet.freezePanes.freezeRows(4);
}

const used = new Set();
const categorySheetNames = new Map();
const summary = workbook.worksheets.add("0_说明与检查");
used.add("0_说明与检查");
summary.showGridLines = false;
summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [[`ETHUSDC 各类止盈${ranking}前${limit}名`]];
summary.getRange("A1:F1").format = { fill: navy, font: { bold: true, color: "#FFFFFF", size: 16 } };
summary.getRange("A1:F1").format.rowHeight = 28;
summary.getRange("A3:F3").values = [["项目", "值", "检查", "状态", "口径", "备注"]];
summary.getRange("A3:F3").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };
const categoryNames = Object.keys(categories);
const totalDataRows = categoryNames.reduce((sum, name) => sum + (categories[name] || []).length, 0);
const largeWorkbook = totalDataRows > 20000;
summary.getRange("A4:F10").values = [
  ["手续费", "按各行配置", "已计入收益", "OK", "各次成交名义金额×净费率", "不在每日页重复扣除；旧零费率结果仍为0"],
  ["止盈类别数", categoryNames.length, categoryNames.length, "OK", "按类别分别排行", `每类最多${limit}名`],
  ["初始资金", Number(payload["初始资金"] ?? 100), Number(payload["初始资金"] ?? 100), "OK", "USDC", "逐笔复利"],
  ["时间口径", "UTC", "UTC", "OK", "已收盘K线", "周六、周日为周末"],
  ["同K线冲突", "止损优先", "止损优先", "OK", "保守规则", "无法知道分钟内先后"],
  ["同指标重复开仓", 0, 0, "OK", "同一1分钟MACD单调段最多一次", ""],
  ["总状态", null, null, null, "", ""],
];
summary.getRange("A11:F11").merge();
summary.getRange("A11").values = [["单位说明：收益率、胜率和回撤在CSV中保存为小数（0.001=0.1%），Excel中统一按百分比显示；资金单位为USDC。"]];
summary.getRange("A11:F11").format = { fill: note, font: { color: "#7F6000" }, wrapText: true };
summary.getRange("B10").formulas = [["=IF(COUNTIF(D4:D9,\"<>OK\")=0,\"OK\",\"检查\")"]];
summary.getRange("D10").formulas = [["=B10"]];
summary.getRange("A3:F10").format.borders = { preset: "all", style: "thin", color: border };
summary.getRange("A4:F4").format.wrapText = true;
summary.getRange("A4:F4").format.rowHeight = 36;
summary.getRange("D4:D10").conditionalFormats.add("containsText", { text: "OK", format: { fill: green, font: { color: "#006100", bold: true } } });
summary.getRange("A12:C12").values = [["工作表", "实际行数", "最多行数"]];
summary.getRange("A12:C12").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };
if (categoryNames.length) {
  summary.getRangeByIndexes(12, 0, categoryNames.length, 3).values = categoryNames.map(name => [name, categories[name].length, limit]);
  summary.getRangeByIndexes(11, 0, categoryNames.length + 1, 3).format.borders = { preset: "all", style: "thin", color: border };
} else {
  summary.getRange("A13:C13").merge();
  summary.getRange("A13").values = [["没有符合当前排行榜门槛的策略；可放宽最低值/最高值后重新从已有CSV导出。"]];
  summary.getRange("A12:C13").format.borders = { preset: "all", style: "thin", color: border };
  summary.getRange("A13:C13").format = { fill: note, font: { color: "#C00000", bold: true }, wrapText: true };
}
summary.getRange("A1:A30").format.columnWidth = 28;
summary.getRange("B1:D30").format.columnWidth = 18;
summary.getRange("E1:F30").format.columnWidth = 34;
summary.freezePanes.freezeRows(3);

for (const category of categoryNames) {
  const rows = categories[category];
  const sheetName = safeSheetName(category, used);
  categorySheetNames.set(category, sheetName);
  const sheet = workbook.worksheets.add(sheetName);
  styleTitle(sheet, `${category}｜${ranking}前${rows.length.toLocaleString("zh-CN")}名`,
    `排行指标：${payload["排行指标"] || "期末资金（USDC）"}；${ranking}排行。请同时检查回撤、交易数及计算版本，历史排名不代表实盘收益。`, headers.length);
  sheet.getRangeByIndexes(3, 0, 1, headers.length).values = [headers];
  if (rows.length) {
    sheet.getRangeByIndexes(4, 0, rows.length, headers.length).values = rows;
    const last = rows.length + 4;
    headers.forEach((header, index) => {
      const range = sheet.getRangeByIndexes(4, index, rows.length, 1);
      if (header.includes("（%）")) range.format.numberFormat = "0.000%";
      else if (header.includes("（USDC）")) range.format.numberFormat = "#,##0.00";
      else if (header.includes("（倍）")) range.format.numberFormat = "0.000";
      else if (header.includes("（分钟）") || header.includes("（单）") || header.includes("（次）")) range.format.numberFormat = "0";
      else if (header.includes("（单/日）")) range.format.numberFormat = "0.00";
      else if (header === "t值") range.format.numberFormat = "0.000";
    });
    for (const header of ["累计收益率（%）", "最大回撤（%）"]) {
      const index = headers.indexOf(header);
      if (index >= 0) sheet.getRangeByIndexes(4, index, rows.length, 1).conditionalFormats.add("colorScale", { colors: ["#F8696B", "#FFEB84", "#63BE7B"], thresholds: ["min", "50%", "max"] });
    }
    addRankColorScales(sheet, headers, 5, rows.length);
  }
  const widths = [12,22,18,14,12,12,12,12,14,12,12,12,12,12,20,30,12,12,12,12,12,16,15,15,15,12,12,16,16,14,14,15,15,18,18,18];
  widths.forEach((width, index) => sheet.getRangeByIndexes(0, index, Math.max(5, rows.length + 4), 1).format.columnWidth = width);
}

if (ranking !== "最差") {
  const daily = workbook.worksheets.add("每日收益测算");
  used.add("每日收益测算");
  daily.showGridLines = false;
  daily.getRange("A1:N1").merge();
  daily.getRange("A1").values = [["各类止盈每日收益——成交成本校正"]];
  daily.getRange("A1:M1").format = { fill: navy, font: { bold: true, color: "#FFFFFF", size: 16 } };
  daily.getRange("A1:N1").format.rowHeight = 28;
  daily.getRange("A3:N3").merge();
  daily.getRange("A3").values = [["按各类第1行完整交易数与名义倍数估算，保留回测已扣手续费，仅替换成交偏移；不是逐日权益收益，不反映数量上限及复利。交易序列固定，不重新模拟手续费变化或开仓间隔。旧分批缺少完整交易数时留空。"]];
  daily.getRange("A3:M3").format = { fill: note, font: { color: "#7F6000", bold: true }, wrapText: true };
  daily.getRange("O3:P3").merge();
  daily.getRange("O3").values = [["成交偏移压力比较（费用不变）"]];
  daily.getRange("O3:P3").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };

  daily.getRange("A5:C5").values = [["参数", "数值", "说明"]];
  daily.getRange("A5:C5").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };
  daily.getRange("A6:C12").values = [
    ["初始资金（USDC）", payload["初始资金"] ?? null, "优先使用各策略行的初始资金"],
    ["开仓偏移情景", 0.00005, "合成示例，用户自行调整，不是保证成交价"],
    ["平仓偏移情景", 0.00005, "合成示例，用户自行调整，不是保证成交价"],
    ["往返偏移情景一", null, "开仓偏移 + 平仓偏移；不含手续费"],
    ["往返偏移情景二", 0.0005, "通用压力示例，用户自行调整，不是实测p95"],
    ["单边偏移压力假设", 0.0005, "仅用于偏移压力示例，不是Taker手续费"],
    ["往返偏移情景三", null, "双边偏移；已扣手续费保持不变"],
  ];
  daily.getRange("B9").formulas = [["=B7+B8"]];
  daily.getRange("B12").formulas = [["=B11*2"]];
  daily.getRange("B7:B12").format.numberFormat = "0.00000%";
  daily.getRange("B6").format.numberFormat = "0.000000";
  daily.getRange("B6:B8").format.font = { color: "#0000FF" };
  daily.getRange("B10:B11").format.font = { color: "#0000FF" };
  daily.getRange("A5:C12").format.borders = { preset: "all", style: "thin", color: border };
  daily.getRange("A13:C13").merge();
  daily.getRange("A13").values = [["先加回原成交偏移，再扣情景偏移；保留已扣手续费，不乘胜率。"]];
  daily.getRange("A13:C13").format = { fill: note, wrapText: true };
  daily.getRange("A13:C13").format.rowHeight = 32;

  daily.getRange("O5:P5").values = [["项目", "结果"]];
  daily.getRange("O5:P5").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };
  daily.getRange("O6:O10").values = [["情景一往返偏移"], ["情景三往返偏移"], ["额外偏移"], ["20x名义倍数下收益差"], ["手续费"]];
  daily.getRange("O11").values = [["交易序列"]];
  daily.getRange("P6").formulas = [["=$B$9"]];
  daily.getRange("P7").formulas = [["=$B$12"]];
  daily.getRange("P8").formulas = [["=P7-P6"]];
  daily.getRange("P9").formulas = [["=P8*20"]];
  daily.getRange("P10:P11").values = [["保持各行已扣净手续费"], ["沿用本次回测，不重新择时"]];
  daily.getRange("P6:P9").format.numberFormat = "0.0000%";
  daily.getRange("O5:P11").format.borders = { preset: "all", style: "thin", color: border };
  daily.getRange("O13:P13").merge();
  daily.getRange("O13").values = [["判断边界"]];
  daily.getRange("O13:P13").format = { fill: navy, font: { bold: true, color: "#FFFFFF" } };
  daily.getRange("O14:P16").merge();
  daily.getRange("O14").values = [["本页不比较Maker/Taker优劣。要比较吃单或挂单，请回到UI分别设置开平仓手续费、BNB、返佣及偏移后重跑；仅改手续费不能模拟盘口排队、未成交和收线延迟。"]];
  daily.getRange("O14:P16").format = { fill: note, wrapText: true, verticalAlignment: "top" };

  const dailyHeaders = ["止盈类别", "扣费后未扣偏移日估算", "偏移情景一日估算", "偏移情景二日估算", "偏移情景三日估算", "初始资金", "情景一日估算金额", "情景二日估算金额", "情景三日估算金额", "平均日完整交易数", "名义倍数", "胜率（仅展示）", "回测单笔净收益率", "回测已计往返偏移"];
  daily.getRange("A15:N15").values = [dailyHeaders];
  daily.getRange("A15:N15").format = { fill: navy, font: { bold: true, color: "#FFFFFF" }, wrapText: true };
  daily.getRange("A15:N15").format.rowHeight = 34;
  const dailyRows = categoryNames.filter(name => (categories[name] || []).length > 0);
  if (dailyRows.length) {
    daily.getRangeByIndexes(15, 0, dailyRows.length, 1).values = dailyRows.map(name => [name]);
  } else {
    daily.getRange("A16:N16").merge();
    daily.getRange("A16").values = [["没有符合当前排行榜门槛的策略，无法生成每日收益测算。"]];
    daily.getRange("A16:N16").format = { fill: note, font: { color: "#C00000", bold: true }, wrapText: true };
  }
  const completeTradesHeader = findHeader("平均日完整交易数（次/日）", "平均日完整交易数（单/日）");
  const legacyOrdersHeader = findHeader("平均日成交订单数（笔/日）", "平均日成交单数（单/日）", "平均日成交单数");
  const sourceHeaders = {
    trades: completeTradesHeader >= 0 ? completeTradesHeader : legacyOrdersHeader,
    multiple: findHeader("名义倍数（倍）", "名义倍数"),
    winRate: findHeader("胜率（%）", "胜率"),
    avgReturn: findHeader("平均单笔收益率（%）", "平均单笔收益率"),
    appliedSlippage: findHeader("往返成交偏移（%）"),
    initial: findHeader("初始资金（USDC）"),
  };
  if ([sourceHeaders.trades, sourceHeaders.multiple, sourceHeaders.avgReturn].some(index => index < 0)) throw new Error("每日收益测算缺少必要排行字段");
  dailyRows.forEach((category, offset) => {
    const row = 16 + offset;
    const sourceSheet = categorySheetNames.get(category).replaceAll("'", "''");
    const unknownPartial = completeTradesHeader < 0 && category === "分批止盈";
    const tradesFormula = completeTradesHeader >= 0
      ? `='${sourceSheet}'!${columnLetter(sourceHeaders.trades)}5`
      : unknownPartial ? '=""' : `='${sourceSheet}'!${columnLetter(sourceHeaders.trades)}5/2`;
    daily.getRange(`J${row}:M${row}`).formulas = [[
      tradesFormula,
      `='${sourceSheet}'!${columnLetter(sourceHeaders.multiple)}5`,
      sourceHeaders.winRate >= 0 ? `='${sourceSheet}'!${columnLetter(sourceHeaders.winRate)}5` : "=0",
      `='${sourceSheet}'!${columnLetter(sourceHeaders.avgReturn)}5`,
    ]];
    if (sourceHeaders.appliedSlippage >= 0) {
      daily.getRange(`N${row}`).formulas = [[`='${sourceSheet}'!${columnLetter(sourceHeaders.appliedSlippage)}5`]];
    } else {
      daily.getRange(`N${row}`).values = [[0]];
    }
    daily.getRange(`B${row}:I${row}`).formulas = [[
      `=IF(J${row}="","",J${row}*(M${row}+N${row})*K${row})`,
      `=IF(J${row}="","",B${row}-J${row}*$B$9*K${row})`,
      `=IF(J${row}="","",B${row}-J${row}*$B$10*K${row})`,
      `=IF(J${row}="","",B${row}-J${row}*$B$12*K${row})`,
      sourceHeaders.initial >= 0 ? `='${sourceSheet}'!${columnLetter(sourceHeaders.initial)}5` : '=IF($B$6="","",$B$6)',
      `=IF(OR(C${row}="",F${row}=""),"",C${row}*F${row})`,
      `=IF(OR(D${row}="",F${row}=""),"",D${row}*F${row})`,
      `=IF(OR(E${row}="",F${row}=""),"",E${row}*F${row})`,
    ]];
  });
  if (dailyRows.length) {
    const last = 15 + dailyRows.length;
    daily.getRange(`B16:E${last}`).format.numberFormat = "0.0000%";
    daily.getRange(`F16:I${last}`).format.numberFormat = "0.000000";
    daily.getRange(`J16:J${last}`).format.numberFormat = "0.00";
    daily.getRange(`K16:K${last}`).format.numberFormat = "0.0";
    daily.getRange(`L16:N${last}`).format.numberFormat = "0.0000%";
    daily.getRange(`A15:N${last}`).format.borders = { preset: "all", style: "thin", color: border };
    daily.getRange(`C16:E${last}`).conditionalFormats.add("colorScale", { colors: ["#F8696B", "#FFEB84", "#63BE7B"], thresholds: ["min", "50%", "max"] });
  }
  [22,18,18,18,16,14,18,18,18,16,12,14,18,18].forEach((width, index) => daily.getRangeByIndexes(0, index, Math.max(20, 16 + dailyRows.length), 1).format.columnWidth = width);
  daily.getRange("O1:O20").format.columnWidth = 26;
  daily.getRange("P1:P20").format.columnWidth = 30;
  daily.freezePanes.freezeRows(15);
}

workbook.recalculate();
const checks = await workbook.inspect({ kind: "table", range: "0_说明与检查!A1:F25", include: "values,formulas", tableMaxRows: 30, tableMaxCols: 8, maxChars: 12000 });
console.log(checks.ndjson);
if (ranking !== "最差") {
  const dailyCheck = await workbook.inspect({ kind: "table", range: "每日收益测算!A1:P30", include: "values,formulas", tableMaxRows: 30, tableMaxCols: 16, maxChars: 18000 });
  console.log(dailyCheck.ndjson);
}
if (!largeWorkbook) {
  const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan" });
  console.log(errors.ndjson);
}

const previewDir = path.join(path.dirname(outputPath), `Excel预览_${path.parse(outputPath).name}`);
await fs.mkdir(previewDir, { recursive: true });
const previewSheets = largeWorkbook
  ? ["0_说明与检查", ...(ranking !== "最差" ? ["每日收益测算"] : [])]
  : ["0_说明与检查", ...Array.from(used).filter(x => x !== "0_说明与检查")];
for (const name of previewSheets) {
  const previewRange = name === "0_说明与检查" ? "A1:F13" : "A1:P20";
  const preview = await workbook.render({ sheetName: name, range: previewRange, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, sheets: categoryNames.length + 1 + (ranking !== "最差" ? 1 : 0) }));
