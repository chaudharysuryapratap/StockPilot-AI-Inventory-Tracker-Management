import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const outputDir = path.resolve(".");
const templatePath = "C:\\Users\\surya\\Downloads\\StockPilot-product-import-template.csv";
const csvPath = path.join(outputDir, "StockPilot-product-import-test-120.csv");
const xlsxPath = path.join(outputDir, "StockPilot-product-import-test-120.xlsx");

const headers = [
  "sku",
  "barcode",
  "name",
  "category",
  "unit_of_measure",
  "cost_price",
  "sell_price",
  "reorder_point",
  "safety_stock",
  "is_perishable",
  "preferred_supplier_id",
];

const catalog = [
  ["RICE", "Basmati Rice", "Grocery", ["1 kg", "5 kg", "10 kg"], ["bag", "bag", "bag"], 92, false],
  ["FLOUR", "Whole Wheat Flour", "Grocery", ["1 kg", "5 kg", "10 kg"], ["bag", "bag", "bag"], 48, false],
  ["OIL", "Sunflower Oil", "Grocery", ["1 L", "2 L", "5 L"], ["bottle", "bottle", "can"], 132, false],
  ["TEA", "Assam Tea", "Beverages", ["100 g", "250 g", "500 g"], ["packet", "packet", "packet"], 96, false],
  ["COFFEE", "Ground Coffee", "Beverages", ["100 g", "250 g", "500 g"], ["pouch", "pouch", "pouch"], 180, false],
  ["SUGAR", "Granulated Sugar", "Grocery", ["1 kg", "2 kg", "5 kg"], ["bag", "bag", "bag"], 52, false],
  ["SALT", "Iodized Salt", "Grocery", ["500 g", "1 kg", "2 kg"], ["packet", "packet", "packet"], 22, false],
  ["CHICKPEA", "Chickpeas", "Grocery", ["500 g", "1 kg", "5 kg"], ["bag", "bag", "bag"], 74, false],
  ["MILK", "Fresh Milk", "Dairy", ["500 ml", "1 L", "2 L"], ["pouch", "bottle", "bottle"], 32, true],
  ["YOGURT", "Greek Yogurt", "Dairy", ["100 g", "400 g", "1 kg"], ["cup", "tub", "tub"], 38, true],
  ["CHEESE", "Cheddar Cheese", "Dairy", ["200 g", "500 g", "1 kg"], ["pack", "pack", "pack"], 155, true],
  ["BUTTER", "Salted Butter", "Dairy", ["100 g", "500 g", "1 kg"], ["pack", "pack", "pack"], 62, true],
  ["EGGS", "Farm Eggs", "Dairy", ["6 count", "12 count", "30 count"], ["tray", "tray", "tray"], 46, true],
  ["BANANA", "Bananas", "Fresh Produce", ["6-piece bunch", "1 kg", "5 kg"], ["bunch", "kg", "box"], 44, true],
  ["APPLE", "Red Apples", "Fresh Produce", ["500 g", "1 kg", "5 kg"], ["punnet", "bag", "box"], 118, true],
  ["TOMATO", "Tomatoes", "Fresh Produce", ["500 g", "1 kg", "5 kg"], ["tray", "bag", "crate"], 35, true],
  ["POTATO", "Potatoes", "Fresh Produce", ["1 kg", "5 kg", "10 kg"], ["bag", "bag", "bag"], 31, true],
  ["ONION", "Red Onions", "Fresh Produce", ["1 kg", "5 kg", "10 kg"], ["bag", "bag", "bag"], 36, true],
  ["OJ", "Orange Juice", "Beverages", ["250 ml", "1 L", "2 L"], ["bottle", "bottle", "bottle"], 58, true],
  ["WATER", "Mineral Water", "Beverages", ["500 ml", "1 L", "20 L"], ["bottle", "bottle", "jar"], 14, false],
  ["COLA", "Cola Drink", "Beverages", ["330 ml", "750 ml", "2 L"], ["can", "bottle", "bottle"], 28, false],
  ["NOODLE", "Instant Noodles", "Grocery", ["single pack", "6-pack", "24-pack case"], ["packet", "pack", "case"], 18, false],
  ["BREAD", "Sandwich Bread", "Bakery", ["400 g", "700 g", "2-loaf pack"], ["loaf", "loaf", "pack"], 34, true],
  ["COOKIE", "Chocolate Cookies", "Bakery", ["100 g", "300 g", "1 kg"], ["packet", "pack", "box"], 42, false],
  ["FPEAS", "Frozen Green Peas", "Frozen Foods", ["250 g", "1 kg", "5 kg"], ["pack", "bag", "bag"], 68, true],
  ["PIZZA", "Frozen Pizza", "Frozen Foods", ["personal", "large", "family 3-pack"], ["piece", "piece", "pack"], 145, true],
  ["DISH", "Dishwashing Liquid", "Household", ["250 ml", "750 ml", "5 L"], ["bottle", "bottle", "can"], 48, false],
  ["DETERGENT", "Laundry Detergent", "Household", ["500 g", "2 kg", "10 kg"], ["pack", "bag", "bag"], 82, false],
  ["FLOOR", "Floor Cleaner", "Household", ["500 ml", "2 L", "5 L"], ["bottle", "bottle", "can"], 76, false],
  ["TOWEL", "Paper Towels", "Household", ["2-roll pack", "6-roll pack", "24-roll case"], ["pack", "pack", "case"], 64, false],
  ["SHAMPOO", "Daily Care Shampoo", "Personal Care", ["100 ml", "500 ml", "1 L"], ["bottle", "bottle", "bottle"], 72, false],
  ["TOOTH", "Mint Toothpaste", "Personal Care", ["50 g", "150 g", "300 g"], ["tube", "tube", "pack"], 38, false],
  ["SOAP", "Hand Soap", "Personal Care", ["100 g bar", "3-bar pack", "12-bar case"], ["piece", "pack", "case"], 24, false],
  ["PEN", "Blue Ballpoint Pens", "Office Supplies", ["single", "10-count box", "50-count box"], ["piece", "box", "box"], 12, false],
  ["NOTE", "A5 Ruled Notebook", "Office Supplies", ["80 pages", "160 pages", "10-pack"], ["piece", "piece", "pack"], 34, false],
  ["PAPER", "A4 Printer Paper", "Office Supplies", ["100 sheets", "500-sheet ream", "5-ream case"], ["pack", "ream", "case"], 86, false],
  ["USBC", "USB-C Cable", "Electronics", ["1 m", "2 m", "5-pack"], ["piece", "piece", "pack"], 118, false],
  ["BULB", "LED Bulb", "Electronics", ["7 W", "12 W", "4-pack"], ["piece", "piece", "pack"], 92, false],
  ["BATT", "AA Batteries", "Electronics", ["2-pack", "8-pack", "24-pack"], ["pack", "pack", "box"], 48, false],
  ["BOX", "Storage Box, Clear", "Storage", ["10 L", "20 L", "50 L"], ["piece", "piece", "piece"], 135, false],
];

function quoteCsv(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function toCsv(rows) {
  return rows.map((row) => row.map(quoteCsv).join(",")).join("\r\n") + "\r\n";
}

function roundMoney(value) {
  return (Math.round(value * 100) / 100).toFixed(2);
}

const rows = [];
let itemNo = 1;
for (const [prefix, baseName, category, variants, units, baseCost, perishable] of catalog) {
  for (let variantIndex = 0; variantIndex < 3; variantIndex += 1) {
    const current = itemNo;
    const factor = [0.72, 1.45, 3.9][variantIndex] * (1 + (current % 5) * 0.025);
    const cost = Math.max(1, baseCost * factor);
    const margin = 1.18 + (current % 7) * 0.045;
    const sell = cost * margin;
    const sku = `TST-${prefix}-${["S", "M", "L"][variantIndex]}-${String(current).padStart(3, "0")}`;
    const barcode = current % 17 === 0 ? "" : String(890300100000 + current);
    const reorderPoint = current % 10 === 0 ? 0 : 5 + ((current * 7) % 56);
    const safetyStock = current % 12 === 0 ? 0 : 2 + ((current * 5) % 23);
    rows.push([
      sku,
      barcode,
      `${baseName} ${variants[variantIndex]}`,
      category,
      units[variantIndex],
      roundMoney(cost),
      roundMoney(sell),
      reorderPoint,
      safetyStock,
      perishable ? "true" : "false",
      "",
    ]);
    itemNo += 1;
  }
}

if (rows.length !== 120) throw new Error(`Expected 120 products, generated ${rows.length}`);

const templateText = await fs.readFile(templatePath, "utf8");
const templateWorkbook = await Workbook.fromCSV(templateText, { sheetName: "Template" });
const templatePreview = await templateWorkbook.render({
  sheetName: "Template",
  autoCrop: "all",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "template-before.png"),
  new Uint8Array(await templatePreview.arrayBuffer()),
);

const csvText = toCsv([headers, ...rows]);
await fs.writeFile(csvPath, `\uFEFF${csvText}`, "utf8");

const workbook = await Workbook.fromCSV(csvText, { sheetName: "Products" });
const sheet = workbook.worksheets.getItem("Products");
const used = sheet.getRange(`A1:K${rows.length + 1}`);

sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);
sheet.getRange("A1:K1").format = {
  fill: "#123B5D",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "outside", style: "medium", color: "#123B5D" },
};
sheet.getRange("A1:K1").format.rowHeight = 32;
sheet.getRange(`A2:K${rows.length + 1}`).format = {
  font: { color: "#1F2937" },
  verticalAlignment: "center",
  borders: {
    insideHorizontal: { style: "thin", color: "#E5E7EB" },
    bottom: { style: "thin", color: "#CBD5E1" },
  },
};
sheet.getRange(`B2:B${rows.length + 1}`).format.numberFormat = "0";
sheet.getRange(`F2:G${rows.length + 1}`).format.numberFormat = "0.00";
sheet.getRange(`H2:I${rows.length + 1}`).format.numberFormat = "0";
sheet.getRange(`F2:I${rows.length + 1}`).format.horizontalAlignment = "right";
sheet.getRange(`J2:K${rows.length + 1}`).format.horizontalAlignment = "center";
sheet.getRange(`J2:J${rows.length + 1}`).conditionalFormats.add("containsText", {
  text: "true",
  format: { fill: "#FEF3C7", font: { color: "#92400E", bold: true } },
});

const widths = [20, 17, 32, 20, 18, 13, 13, 16, 14, 15, 22];
for (let col = 0; col < widths.length; col += 1) {
  sheet.getRangeByIndexes(0, col, rows.length + 1, 1).format.columnWidth = widths[col];
}
sheet.getRange(`A2:K${rows.length + 1}`).format.rowHeight = 20;
sheet.tables.add(`A1:K${rows.length + 1}`, true, "ProductsImportTable");

const inspect = await workbook.inspect({
  kind: "table",
  range: "Products!A1:K8",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 11,
  maxChars: 5000,
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
  sheetName: "Products",
  range: "A1:K18",
  scale: 1.2,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "products-preview.png"),
  new Uint8Array(await preview.arrayBuffer()),
);

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(xlsxPath);

const uniqueSkus = new Set(rows.map((row) => row[0]));
const nonBlankBarcodes = rows.map((row) => row[1]).filter(Boolean);
const uniqueBarcodes = new Set(nonBlankBarcodes);
const categoryCounts = Object.fromEntries(
  [...new Set(rows.map((row) => row[3]))].sort().map((category) => [
    category,
    rows.filter((row) => row[3] === category).length,
  ]),
);

const validation = {
  rowCount: rows.length,
  columnCount: headers.length,
  uniqueSkuCount: uniqueSkus.size,
  nonBlankBarcodeCount: nonBlankBarcodes.length,
  uniqueNonBlankBarcodeCount: uniqueBarcodes.size,
  perishableTrueCount: rows.filter((row) => row[9] === "true").length,
  zeroReorderPointCount: rows.filter((row) => Number(row[7]) === 0).length,
  zeroSafetyStockCount: rows.filter((row) => Number(row[8]) === 0).length,
  blankSupplierCount: rows.filter((row) => row[10] === "").length,
  categoryCounts,
};
await fs.writeFile(path.join(outputDir, "validation-summary.json"), JSON.stringify(validation, null, 2));
console.log("VALIDATION_SUMMARY");
console.log(JSON.stringify(validation, null, 2));
console.log(`CSV=${csvPath}`);
console.log(`XLSX=${xlsxPath}`);
