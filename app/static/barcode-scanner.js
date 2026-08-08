(() => {
  "use strict";

  const app = document.getElementById("scanner-app");
  if (!app) return;

  const startButton = document.getElementById("start-scanner");
  const stopButton = document.getElementById("stop-scanner");
  const status = document.getElementById("camera-status");
  const message = document.getElementById("scanner-message");
  const manualForm = document.getElementById("manual-barcode-form");
  const manualInput = document.getElementById("manual-barcode");
  const resultPanel = document.getElementById("scanner-result");
  const scanAnother = document.getElementById("scan-another");
  const transferLink = document.getElementById("transfer-scanned-item");
  const stockBody = document.getElementById("result-stock");
  let scanner = null;
  let running = false;
  let lastScan = { code: "", at: 0 };

  function setState(text, kind = "neutral") {
    status.textContent = text;
    status.className = `status-chip ${kind}`;
  }

  function showMessage(text, kind = "") {
    message.textContent = text;
    message.className = `scanner-message ${kind}`.trim();
  }

  async function stopCamera() {
    if (!scanner || !running) return;
    try {
      await scanner.stop();
    } finally {
      running = false;
      startButton.disabled = false;
      stopButton.disabled = true;
      setState("Camera idle");
    }
  }

  async function lookUp(code) {
    const normalized = String(code || "").trim();
    if (!normalized) return;
    showMessage(`Looking up ${normalized}…`);
    setState("Checking", "working");
    try {
      const url = new URL(app.dataset.lookupUrl, window.location.origin);
      url.searchParams.set("code", normalized);
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "The barcode lookup failed.");
      renderProduct(payload.product, payload.matched_by);
      setState("Item found", "ready");
      showMessage(`Matched by ${payload.matched_by}.`, "success");
      if (navigator.vibrate) navigator.vibrate(80);
    } catch (error) {
      resultPanel.hidden = true;
      setState("No match", "error");
      showMessage(error.message || "The barcode lookup failed.", "error");
    }
  }

  function addCell(row, value, strong = false) {
    const cell = document.createElement("td");
    const content = strong ? document.createElement("strong") : document.createTextNode(value);
    if (strong) {
      content.textContent = value;
      cell.appendChild(content);
    } else {
      cell.appendChild(content);
    }
    row.appendChild(cell);
  }

  function renderProduct(product) {
    document.getElementById("result-name").textContent = product.name;
    document.getElementById("result-category").textContent = product.category;
    document.getElementById("result-sku").textContent = product.sku;
    document.getElementById("result-barcode").textContent = product.barcode || "Not assigned";
    document.getElementById("result-unit").textContent = product.unit_of_measure;
    document.getElementById("result-available").textContent = `${product.totals.quantity_available} ${product.unit_of_measure}`;
    stockBody.replaceChildren();

    if (!product.stock.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.className = "empty-row";
      cell.textContent = "This product has no stock positions yet.";
      row.appendChild(cell);
      stockBody.appendChild(row);
    } else {
      product.stock.forEach((stock) => {
        const row = document.createElement("tr");
        addCell(row, `${stock.location}${stock.bin ? ` / ${stock.bin}` : ""}`, true);
        addCell(row, String(stock.quantity_on_hand));
        addCell(row, String(stock.quantity_reserved));
        addCell(row, String(stock.quantity_available));
        stockBody.appendChild(row);
      });
    }

    const transferUrl = new URL(app.dataset.transferUrl, window.location.origin);
    transferUrl.searchParams.set("sku", product.sku);
    transferLink.href = transferUrl.toString();
    resultPanel.hidden = false;
    resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function onScanSuccess(decodedText) {
    const now = Date.now();
    if (decodedText === lastScan.code && now - lastScan.at < 2500) return;
    lastScan = { code: decodedText, at: now };
    await stopCamera();
    await lookUp(decodedText);
  }

  async function startCamera() {
    if (!window.isSecureContext && !["localhost", "127.0.0.1"].includes(location.hostname)) {
      showMessage("Camera access requires HTTPS (or localhost during development).", "error");
      setState("HTTPS required", "error");
      return;
    }
    if (typeof window.Html5Qrcode !== "function") {
      showMessage("The scanner library did not load. You can still enter a barcode manually.", "error");
      setState("Scanner unavailable", "error");
      return;
    }

    startButton.disabled = true;
    setState("Requesting camera", "working");
    showMessage("Allow camera access when your browser asks.");
    scanner = scanner || new window.Html5Qrcode("barcode-reader");
    try {
      await scanner.start(
        { facingMode: "environment" },
        { fps: 10, qrbox: { width: 280, height: 150 }, aspectRatio: 1.7778 },
        onScanSuccess,
        () => {}
      );
      running = true;
      stopButton.disabled = false;
      setState("Scanning", "ready");
      showMessage("Hold the barcode steady inside the frame.");
    } catch (error) {
      startButton.disabled = false;
      setState("Camera blocked", "error");
      showMessage("Camera access failed. Check browser permission or use manual entry.", "error");
    }
  }

  startButton.addEventListener("click", startCamera);
  stopButton.addEventListener("click", stopCamera);
  manualForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await stopCamera();
    await lookUp(manualInput.value);
  });
  scanAnother.addEventListener("click", () => {
    resultPanel.hidden = true;
    manualInput.value = "";
    startCamera();
  });
  window.addEventListener("pagehide", () => {
    if (scanner && running) scanner.stop().catch(() => {});
  });
})();
