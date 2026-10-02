import fs from "node:fs/promises";

export const COLOR_SCALES = JSON.parse(await fs.readFile(new URL("./rank_color_scales.json", import.meta.url), "utf8"));

// Only data rows are colored. Frequency and leverage are magnitude, not quality.
export function addRankColorScales(sheet, headers, firstRow, rowCount) {
  if (rowCount < 1) return;
  headers.forEach((header, index) => {
    const spec = COLOR_SCALES.find(item => item.headers.includes(header));
    if (spec) sheet.getRangeByIndexes(firstRow - 1, index, rowCount, 1).conditionalFormats.add("colorScale", {
      colors: spec.colors, thresholds: spec.thresholds,
    });
  });
}
