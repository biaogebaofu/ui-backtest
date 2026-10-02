import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const file = process.argv[2];
if (!file) throw new Error("请传入xlsx路径");
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(file));
const sheets = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 8000 });
console.log(sheets.ndjson);
const checks = await workbook.inspect({ kind: "table", range: "0_说明与检查!A1:F25", include: "values,formulas", tableMaxRows: 30, tableMaxCols: 8, maxChars: 12000 });
console.log(checks.ndjson);
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 300 }, summary: "exported workbook formula error scan" });
console.log(errors.ndjson);
