(() => {
  "use strict";

  const chart = document.getElementById("forecast-chart");
  const sourceRows = chart
    ? Array.from(chart.querySelectorAll(".chart-data")).map((item) => ({
        label: item.dataset.label,
        sku: item.dataset.sku,
        demand: Number(item.dataset.demand) || 0,
        reorder: Number(item.dataset.reorder) || 0,
        risk: item.dataset.risk,
      }))
    : [];

  const SVG_NS = "http://www.w3.org/2000/svg";
  let activeFilter = "all";
  let tooltip = null;

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    return element;
  }

  function showTooltip(event, row) {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      document.body.appendChild(tooltip);
    }
    tooltip.innerHTML = `<strong>${row.label}</strong><br>${row.sku}<br>Forecast: ${row.demand}/day<br>Reorder: ${row.reorder}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(event.clientX + 12, window.innerWidth - 225)}px`;
    tooltip.style.top = `${event.clientY + 12}px`;
  }

  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
  }

  function renderChart() {
    if (!chart || !sourceRows.length) return;
    const rows = sourceRows.filter((row) => activeFilter === "all" || row.risk === activeFilter);
    chart.querySelectorAll("svg, .chart-empty-filter").forEach((item) => item.remove());
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state chart-empty-filter";
      empty.innerHTML = '<span>⌕</span><h3>No items in this view</h3><p>Choose another demand filter.</p>';
      chart.appendChild(empty);
      return;
    }

    const width = 760;
    const labelWidth = 150;
    const plotWidth = width - labelWidth - 38;
    const rowHeight = 44;
    const height = rows.length * rowHeight + 48;
    const maxValue = Math.max(1, ...rows.flatMap((row) => [row.demand, row.reorder]));
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

    const legend = [
      { label: "Daily demand", color: "#146C64", x: labelWidth },
      { label: "Reorder quantity", color: "#E8A33D", x: labelWidth + 120 },
    ];
    legend.forEach((item) => {
      svg.appendChild(svgElement("circle", { cx: item.x, cy: 10, r: 4, fill: item.color }));
      const label = svgElement("text", { x: item.x + 9, y: 14, fill: "#69716D", "font-size": 11 });
      label.textContent = item.label;
      svg.appendChild(label);
    });

    rows.forEach((row, index) => {
      const y = 34 + index * rowHeight;
      const group = svgElement("g", { tabindex: "0", role: "button", "aria-label": `${row.label}: daily demand ${row.demand}; reorder ${row.reorder}` });
      const name = svgElement("text", { x: 0, y: y + 12, fill: "#1F2623", "font-size": 12, "font-weight": 700 });
      name.textContent = row.label.length > 19 ? `${row.label.slice(0, 18)}…` : row.label;
      const sku = svgElement("text", { x: 0, y: y + 27, fill: "#69716D", "font-size": 9 });
      sku.textContent = row.sku;
      group.append(name, sku);

      group.appendChild(svgElement("rect", { x: labelWidth, y, width: plotWidth, height: 9, rx: 4.5, fill: "#EEF1F0" }));
      group.appendChild(svgElement("rect", { x: labelWidth, y: y + 16, width: plotWidth, height: 9, rx: 4.5, fill: "#EEF1F0" }));
      group.appendChild(svgElement("rect", { x: labelWidth, y, width: Math.max(row.demand ? 3 : 0, (row.demand / maxValue) * plotWidth), height: 9, rx: 4.5, fill: "#146C64" }));
      group.appendChild(svgElement("rect", { x: labelWidth, y: y + 16, width: Math.max(row.reorder ? 3 : 0, (row.reorder / maxValue) * plotWidth), height: 9, rx: 4.5, fill: row.risk === "risk" ? "#C0432B" : "#E8A33D" }));

      const value = svgElement("text", { x: width - 4, y: y + 20, fill: row.risk === "risk" ? "#C0432B" : "#5C8A3A", "font-size": 10, "font-weight": 800, "text-anchor": "end" });
      value.textContent = row.reorder > 0 ? `${row.reorder} to order` : "covered";
      group.appendChild(value);
      group.addEventListener("pointermove", (event) => showTooltip(event, row));
      group.addEventListener("pointerleave", hideTooltip);
      group.addEventListener("focus", (event) => showTooltip({ clientX: event.target.getBoundingClientRect().right, clientY: event.target.getBoundingClientRect().top }, row));
      group.addEventListener("blur", hideTooltip);
      svg.appendChild(group);
    });
    chart.appendChild(svg);
  }

  document.querySelectorAll("[data-chart-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.chartFilter;
      document.querySelectorAll("[data-chart-filter]").forEach((item) => item.classList.toggle("active", item === button));
      renderChart();
    });
  });
  renderChart();

  const gauge = document.querySelector(".accuracy-gauge");
  if (gauge) {
    const mape = Number(gauge.dataset.mape);
    const hasScore = gauge.dataset.mape !== "" && Number.isFinite(mape);
    const score = hasScore ? Math.max(0, Math.min(100, Math.round(100 - mape))) : 0;
    const ring = gauge.querySelector(".gauge-ring");
    ring?.style.setProperty("--value", score);
    const label = ring?.querySelector("span");
    if (label) label.textContent = hasScore ? `${score}%` : "—";
  }
})();
