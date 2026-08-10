import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const outputDir = path.resolve(".");
const templatePath = "C:\\Users\\surya\\Downloads\\StockPilot-product-import-template (2).csv";
const csvPath = path.join(outputDir, "StockPilot-hardware-Indiranagar-A-840-LKO-120.csv");
const xlsxPath = path.join(outputDir, "StockPilot-hardware-Indiranagar-A-840-LKO-120.xlsx");

const headers = [
  "sku", "barcode", "name", "category", "unit_of_measure", "cost_price", "sell_price",
  "reorder_point", "safety_stock", "is_perishable", "preferred_supplier_id",
  "location_code", "bin_code", "quantity_on_hand",
];

const catalog = [
  ["WSC", "Wood Screws", "Fasteners", ["25 mm - 50 pack", "50 mm - 50 pack", "75 mm - 100 pack"], ["box", "box", "box"], 78],
  ["MSB", "Machine Bolts", "Fasteners", ["M6 x 30 mm - 20 pack", "M8 x 50 mm - 20 pack", "M10 x 80 mm - 25 pack"], ["box", "box", "box"], 115],
  ["HXN", "Hex Nuts", "Fasteners", ["M6 - 50 pack", "M8 - 50 pack", "M10 - 100 pack"], ["box", "box", "box"], 82],
  ["WSH", "Flat Washers", "Fasteners", ["M6 - 50 pack", "M8 - 100 pack", "M10 - 200 pack"], ["pack", "pack", "box"], 48],
  ["NAIL", "Common Nails", "Fasteners", ["25 mm - 500 g", "50 mm - 1 kg", "75 mm - 5 kg"], ["box", "box", "box"], 72],
  ["ANCH", "Wall Anchors", "Fasteners", ["6 mm - 25 pack", "8 mm - 50 pack", "10 mm - 100 pack"], ["pack", "box", "box"], 65],
  ["HAM", "Claw Hammer", "Hand Tools", ["250 g", "500 g", "750 g fiberglass handle"], ["piece", "piece", "piece"], 245],
  ["SDR", "Screwdriver Set", "Hand Tools", ["2-piece", "6-piece", "12-piece precision set"], ["set", "set", "set"], 180],
  ["PLR", "Combination Pliers", "Hand Tools", ["150 mm", "200 mm", "250 mm insulated"], ["piece", "piece", "piece"], 215],
  ["WRN", "Adjustable Wrench", "Hand Tools", ["150 mm", "250 mm", "350 mm"], ["piece", "piece", "piece"], 260],
  ["CHSL", "Cold Chisel", "Hand Tools", ["150 mm", "250 mm", "3-piece set"], ["piece", "piece", "set"], 135],
  ["TAPE", "Measuring Tape", "Hand Tools", ["3 m", "5 m", "10 m"], ["piece", "piece", "piece"], 95],
  ["KNIFE", "Utility Knife", "Hand Tools", ["9 mm", "18 mm", "heavy-duty with 10 blades"], ["piece", "piece", "set"], 68],
  ["HSAW", "Hand Saw", "Hand Tools", ["300 mm", "450 mm", "600 mm professional"], ["piece", "piece", "piece"], 225],
  ["DRILL", "Electric Drill", "Power Tools", ["450 W 10 mm", "650 W 13 mm impact", "850 W 13 mm impact kit"], ["piece", "piece", "kit"], 1650],
  ["GRIND", "Angle Grinder", "Power Tools", ["650 W 100 mm", "900 W 115 mm", "1400 W 125 mm kit"], ["piece", "piece", "kit"], 1850],
  ["CSAW", "Circular Saw", "Power Tools", ["1200 W 165 mm", "1500 W 185 mm", "1800 W 235 mm"], ["piece", "piece", "piece"], 3250],
  ["JSAW", "Jigsaw", "Power Tools", ["400 W", "650 W variable speed", "750 W orbital kit"], ["piece", "piece", "kit"], 2200],
  ["SAND", "Orbital Sander", "Power Tools", ["180 W", "300 W variable speed", "450 W dust extraction kit"], ["piece", "piece", "kit"], 1900],
  ["WIRE", "Copper Electrical Wire", "Electrical", ["1.0 sq mm - 10 m", "1.5 sq mm - 90 m", "2.5 sq mm - 90 m"], ["roll", "coil", "coil"], 520],
  ["SWIT", "Modular Switch", "Electrical", ["6 A 1-way", "16 A 1-way", "20 A double-pole"], ["piece", "piece", "piece"], 75],
  ["SOCK", "Electrical Socket", "Electrical", ["6 A 2-pin", "6/16 A universal", "32 A industrial"], ["piece", "piece", "piece"], 95],
  ["MCB", "Miniature Circuit Breaker", "Electrical", ["6 A single-pole", "20 A single-pole", "32 A double-pole"], ["piece", "piece", "piece"], 185],
  ["EXT", "Extension Board", "Electrical", ["3 socket 2 m", "4 socket 5 m", "6 socket 10 m surge protected"], ["piece", "piece", "piece"], 345],
  ["CTIE", "Nylon Cable Ties", "Electrical", ["100 mm - 100 pack", "200 mm - 100 pack", "300 mm - 200 pack"], ["pack", "pack", "box"], 58],
  ["ITAPE", "PVC Insulation Tape", "Electrical", ["10 m black", "20 m assorted 5-pack", "20 m assorted 20-pack"], ["roll", "pack", "box"], 24],
  ["PVC", "PVC Pipe", "Plumbing", ["20 mm x 3 m", "32 mm x 3 m", "50 mm x 6 m"], ["length", "length", "length"], 165],
  ["ELB", "PVC Elbow", "Plumbing", ["20 mm - 10 pack", "32 mm - 10 pack", "50 mm - 20 pack"], ["pack", "pack", "box"], 70],
  ["VALVE", "Brass Ball Valve", "Plumbing", ["15 mm", "25 mm", "40 mm"], ["piece", "piece", "piece"], 285],
  ["TAP", "Bib Tap", "Plumbing", ["PVC 15 mm", "brass 15 mm", "chrome-plated 20 mm"], ["piece", "piece", "piece"], 195],
  ["PTFE", "PTFE Thread Seal Tape", "Plumbing", ["12 mm x 10 m", "19 mm x 15 m - 5 pack", "25 mm x 15 m - 20 pack"], ["roll", "pack", "box"], 22],
  ["HCLP", "Stainless Hose Clamps", "Plumbing", ["12-20 mm - 10 pack", "20-32 mm - 20 pack", "32-50 mm - 50 pack"], ["pack", "box", "box"], 85],
  ["PAINT", "Interior Emulsion Paint", "Paint & Adhesives", ["1 L white", "4 L white", "20 L white"], ["can", "can", "bucket"], 320],
  ["PRIME", "Wall Primer", "Paint & Adhesives", ["1 L", "4 L", "20 L"], ["can", "can", "bucket"], 250],
  ["BRUSH", "Paint Brush", "Paint & Adhesives", ["25 mm", "50 mm", "100 mm - 6 pack"], ["piece", "piece", "pack"], 58],
  ["ROLLER", "Paint Roller", "Paint & Adhesives", ["100 mm", "225 mm", "225 mm 5-piece kit"], ["piece", "piece", "kit"], 105],
  ["SEAL", "Silicone Sealant", "Paint & Adhesives", ["100 ml clear", "280 ml white", "280 ml assorted 12-pack"], ["tube", "cartridge", "box"], 145],
  ["EPOXY", "Two-Part Epoxy Adhesive", "Paint & Adhesives", ["25 g", "100 g", "1 kg industrial kit"], ["pack", "pack", "kit"], 95],
  ["GLOVE", "Work Gloves", "Safety Equipment", ["cotton grip", "nitrile-coated", "cut-resistant level 5"], ["pair", "pair", "pair"], 85],
  ["GLASS", "Safety Glasses", "Safety Equipment", ["clear lens", "smoke lens", "anti-fog 10-pack"], ["piece", "piece", "box"], 120],
];

function quoteCsv(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function toCsv(rows) {
  return rows.map((row) => row.map(quoteCsv).join(",")).join("\r\n") + "\r\n";
}

function money(value) {
  return (Math.round(value * 100) / 100).toFixed(2);
}

const rows = [];
let itemNo = 1;
for (const [prefix, baseName, category, variants, units, baseCost] of catalog) {
  for (let variantIndex = 0; variantIndex < 3; variantIndex += 1) {
    const current = itemNo;
    const factor = [0.72, 1.38, 3.25][variantIndex] * (1 + (current % 6) * 0.03);
    const cost = Math.max(2, baseCost * factor);
    const margin = 1.22 + (current % 8) * 0.035;
    const sku = `HW-${prefix}-${["S", "M", "L"][variantIndex]}-${String(current).padStart(3, "0")}`;
    const barcode = current % 19 === 0 ? "" : String(891100200000 + current);
    const reorderPoint = current % 11 === 0 ? 0 : 4 + ((current * 7) % 48);
    const safetyStock = current % 13 === 0 ? 0 : 2 + ((current * 3) % 18);
    const openingQuantity = current % 23 === 0 ? 0 : 8 + ((current * 7) % 39);
    rows.push([
      sku,
      barcode,
      `${baseName} ${variants[variantIndex]}`,
      category,
      units[variantIndex],
      money(cost),
      money(cost * margin),
      reorderPoint,
      safetyStock,
      "false",
      "",
      "INDIRANAGAR",
      "A-840-LKO",
      openingQuantity,
    ]);
    itemNo += 1;
  }
}

if (rows.length !== 120) throw new Error(`Expected 120 products, generated ${rows.length}`);

const templateText = await fs.readFile(templatePath, "utf8");
const templateWorkbook = await Workbook.fromCSV(templateText, { sheetName: "Template" });
const templatePreview = await templateWorkbook.render({
  sheetName: "Template", autoCrop: "all", scale: 1.4, format: "png",
});
await fs.writeFile(
  path.join(outputDir, "template-before.png"),
  new Uint8Array(await templatePreview.arrayBuffer()),
);

const csvText = toCsv([headers, ...rows]);
await fs.writeFile(csvPath, `\uFEFF${csvText}`, "utf8");

const workbook = await Workbook.fromCSV(csvText, { sheetName: "Hardware Products" });
const sheet = workbook.worksheets.getItem("Hardware Products");
const lastRow = rows.length + 1;

sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);
sheet.getRange("A1:N1").format = {
  fill: "#713F12",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "outside", style: "medium", color: "#713F12" },
};
sheet.getRange("A1:N1").format.rowHeight = 34;
sheet.getRange(`A2:N${lastRow}`).format = {
  font: { color: "#292524" },
  verticalAlignment: "center",
  borders: {
    insideHorizontal: { style: "thin", color: "#E7E5E4" },
    bottom: { style: "thin", color: "#D6D3D1" },
  },
};
sheet.getRange(`B2:B${lastRow}`).format.numberFormat = "0";
sheet.getRange(`F2:G${lastRow}`).format.numberFormat = "0.00";
sheet.getRange(`H2:I${lastRow}`).format.numberFormat = "0";
sheet.getRange(`N2:N${lastRow}`).format.numberFormat = "0";
sheet.getRange(`F2:I${lastRow}`).format.horizontalAlignment = "right";
sheet.getRange(`J2:N${lastRow}`).format.horizontalAlignment = "center";
const widths = [19, 17, 38, 20, 18, 13, 13, 16, 14, 15, 22, 15, 13, 18];
for (let col = 0; col < widths.length; col += 1) {
  sheet.getRangeByIndexes(0, col, lastRow, 1).format.columnWidth = widths[col];
}
sheet.getRange(`A2:N${lastRow}`).format.rowHeight = 20;
sheet.tables.add(`A1:N${lastRow}`, true, "HardwareImportTable");

const inspect = await workbook.inspect({
  kind: "table",
  range: "'Hardware Products'!A1:N8",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 14,
  maxChars: 6000,
});
console.log("DATA_INSPECT");
console.log(inspect.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log("ERROR_SCAN");
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Hardware Products",
  range: "A1:N18",
  scale: 1.15,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "hardware-products-Indiranagar-preview.png"),
  new Uint8Array(await preview.arrayBuffer()),
);

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(xlsxPath);

const nonBlankBarcodes = rows.map((row) => row[1]).filter(Boolean);
const summary = {
  rowCount: rows.length,
  columnCount: headers.length,
  uniqueSkuCount: new Set(rows.map((row) => row[0])).size,
  nonBlankBarcodeCount: nonBlankBarcodes.length,
  uniqueNonBlankBarcodeCount: new Set(nonBlankBarcodes).size,
  categoryCount: new Set(rows.map((row) => row[3])).size,
  zeroOpeningStockCount: rows.filter((row) => Number(row[13]) === 0).length,
  totalOpeningStock: rows.reduce((total, row) => total + Number(row[13]), 0),
  locationCodes: [...new Set(rows.map((row) => row[11]))],
  binCodes: [...new Set(rows.map((row) => row[12]))],
  binCapacity: 4522,
  blankSupplierCount: rows.filter((row) => row[10] === "").length,
  blankBinCount: rows.filter((row) => row[12] === "").length,
};
await fs.writeFile(path.join(outputDir, "validation-summary.json"), JSON.stringify(summary, null, 2));
console.log("VALIDATION_SUMMARY");
console.log(JSON.stringify(summary, null, 2));
console.log(`CSV=${csvPath}`);
console.log(`XLSX=${xlsxPath}`);
