(() => {
  "use strict";

  const body = document.body;
  const sidebar = document.getElementById("primary-sidebar");
  const scrim = document.getElementById("mobile-scrim");
  const openers = [
    document.getElementById("menu-button"),
    document.getElementById("mobile-more"),
  ].filter(Boolean);
  const closeButton = document.getElementById("sidebar-close");

  function setNavigation(open) {
    body.classList.toggle("nav-open", open);
    openers.forEach((button) => button.setAttribute("aria-expanded", String(open)));
    if (scrim) scrim.setAttribute("aria-hidden", String(!open));
    if (open && sidebar) sidebar.querySelector("a")?.focus();
  }

  openers.forEach((button) => {
    button.setAttribute("aria-controls", "primary-sidebar");
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", () => setNavigation(true));
  });
  closeButton?.addEventListener("click", () => setNavigation(false));
  scrim?.addEventListener("click", () => setNavigation(false));

  const command = document.getElementById("command-palette");
  const commandTrigger = document.getElementById("command-trigger");
  const commandClose = document.getElementById("command-close");
  const commandInput = document.getElementById("command-input");
  const commandEmpty = document.getElementById("command-empty");
  const commandLinks = Array.from(document.querySelectorAll("[data-command]"));

  function filterCommands() {
    const query = (commandInput?.value || "").trim().toLowerCase();
    let visible = 0;
    commandLinks.forEach((link) => {
      const matches = `${link.dataset.command} ${link.textContent}`.toLowerCase().includes(query);
      link.hidden = !matches;
      if (matches) visible += 1;
    });
    if (commandEmpty) commandEmpty.hidden = visible > 0;
  }

  function openCommand() {
    if (!command || typeof command.showModal !== "function") return;
    if (!command.open) command.showModal();
    commandInput?.focus();
    commandInput?.select();
  }

  commandTrigger?.addEventListener("click", openCommand);
  commandClose?.addEventListener("click", () => command?.close());
  commandInput?.addEventListener("input", filterCommands);
  command?.addEventListener("click", (event) => {
    if (event.target === command) command.close();
  });

  const profileMenu = document.getElementById("profile-menu");
  document.addEventListener("click", (event) => {
    if (profileMenu?.open && !profileMenu.contains(event.target)) {
      profileMenu.removeAttribute("open");
    }
  });

  document.querySelectorAll("[data-auto-submit]").forEach((control) => {
    control.addEventListener("change", () => control.form?.requestSubmit());
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openCommand();
    }
    if (event.key === "Escape" && body.classList.contains("nav-open")) {
      setNavigation(false);
    }
  });

  function cellValue(row, index) {
    return (row.cells[index]?.innerText || "").trim().replace(/\s+/g, " ");
  }

  function comparable(value) {
    const normalized = value.replace(/[,₹$£€%]/g, "").trim();
    if (/^-?\d+(\.\d+)?(?:\s|$)/.test(normalized)) {
      const number = Number.parseFloat(normalized);
      if (Number.isFinite(number)) return { type: "number", value: number };
    }
    const date = Date.parse(value);
    if (/\d{4}-\d{2}-\d{2}/.test(value) && Number.isFinite(date)) {
      return { type: "number", value: date };
    }
    return { type: "text", value: value.toLocaleLowerCase() };
  }

  function enhanceTable(table, tableIndex) {
    const tbody = table.tBodies[0];
    const rows = tbody ? Array.from(tbody.rows).filter((row) => !row.querySelector(".empty-row")) : [];
    const headers = Array.from(table.tHead?.rows[0]?.cells || []);
    const wrap = table.closest(".table-wrap");
    if (!tbody || !wrap || !headers.length || table.closest(".po-card") || rows.length < 2) return;

    table.classList.add("interactive-table");
    const toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    const search = document.createElement("label");
    search.className = "table-search";
    search.innerHTML = `<span class="sr-only">Search this table</span><input type="search" placeholder="Search ${rows.length} rows…" aria-label="Search table">`;
    toolbar.appendChild(search);

    const filterHeadings = ["status", "risk", "role", "location", "category"];
    const filterIndex = headers.findIndex((header) => filterHeadings.includes(header.innerText.trim().toLowerCase()));
    let filter = null;
    if (filterIndex >= 0) {
      const values = [...new Set(rows.map((row) => cellValue(row, filterIndex)).filter(Boolean))].sort();
      if (values.length > 1 && values.length <= 15) {
        filter = document.createElement("select");
        filter.className = "table-filter";
        filter.setAttribute("aria-label", `Filter by ${headers[filterIndex].innerText.trim()}`);
        filter.innerHTML = `<option value="">All ${headers[filterIndex].innerText.trim().toLowerCase()}</option>`;
        values.forEach((value) => filter.add(new Option(value, value)));
        toolbar.appendChild(filter);
      }
    }

    const count = document.createElement("span");
    count.className = "table-count";
    toolbar.appendChild(count);
    wrap.before(toolbar);

    const noResults = document.createElement("div");
    noResults.className = "empty-state table-no-results";
    noResults.hidden = true;
    noResults.innerHTML = `<span>⌕</span><h3>No matching rows</h3><p>Try a broader search or clear the filter.</p>`;
    wrap.after(noResults);

    function applyFilters() {
      const query = search.querySelector("input").value.trim().toLocaleLowerCase();
      const selected = filter?.value || "";
      let visible = 0;
      rows.forEach((row) => {
        const matchesSearch = !query || row.innerText.toLocaleLowerCase().includes(query);
        const matchesFilter = !selected || cellValue(row, filterIndex) === selected;
        row.hidden = !(matchesSearch && matchesFilter);
        if (!row.hidden) visible += 1;
      });
      count.textContent = `${visible} of ${rows.length}`;
      wrap.hidden = visible === 0;
      noResults.hidden = visible !== 0;
    }

    search.querySelector("input").addEventListener("input", applyFilters);
    filter?.addEventListener("change", applyFilters);
    applyFilters();

    headers.forEach((header, columnIndex) => {
      if (header.dataset.sortable === "false") return;
      header.dataset.sortable = "true";
      header.tabIndex = 0;
      header.setAttribute("role", "button");
      header.setAttribute("aria-label", `Sort by ${header.innerText.trim()}`);
      const sort = () => {
        const ascending = !header.classList.contains("sort-asc");
        headers.forEach((item) => item.classList.remove("sort-asc", "sort-desc"));
        header.classList.add(ascending ? "sort-asc" : "sort-desc");
        rows.sort((left, right) => {
          const a = comparable(cellValue(left, columnIndex));
          const b = comparable(cellValue(right, columnIndex));
          const result = a.type === "number" && b.type === "number"
            ? a.value - b.value
            : String(a.value).localeCompare(String(b.value), undefined, { numeric: true });
          return ascending ? result : -result;
        }).forEach((row) => tbody.appendChild(row));
      };
      header.addEventListener("click", sort);
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sort();
        }
      });
    });
  }

  document.querySelectorAll(".table-wrap table").forEach(enhanceTable);

  const bulkProductForm = document.querySelector("[data-bulk-product-form]");
  const selectAllProducts = document.getElementById("select-all-products");
  const productCheckboxes = Array.from(document.querySelectorAll("[data-product-row-checkbox]"));
  const removeSelectedButton = document.querySelector("[data-remove-selected]");
  const productSelectionCount = document.getElementById("product-selection-count");

  if (bulkProductForm && selectAllProducts && removeSelectedButton && productSelectionCount) {
    function syncProductSelection() {
      const selected = productCheckboxes.filter((checkbox) => checkbox.checked);
      productCheckboxes.forEach((checkbox) => {
        checkbox.closest("tr")?.classList.toggle("is-selected", checkbox.checked);
      });
      productSelectionCount.textContent = `${selected.length} selected`;
      removeSelectedButton.disabled = selected.length === 0;
      selectAllProducts.disabled = productCheckboxes.length === 0;
      selectAllProducts.checked = productCheckboxes.length > 0 && selected.length === productCheckboxes.length;
      selectAllProducts.indeterminate = selected.length > 0 && selected.length < productCheckboxes.length;
    }

    selectAllProducts.addEventListener("change", () => {
      productCheckboxes.forEach((checkbox) => {
        checkbox.checked = selectAllProducts.checked;
      });
      syncProductSelection();
    });
    productCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", syncProductSelection));
    bulkProductForm.addEventListener("submit", (event) => {
      const selectedCount = productCheckboxes.filter((checkbox) => checkbox.checked).length;
      if (!selectedCount) {
        event.preventDefault();
        syncProductSelection();
        return;
      }
      const noun = selectedCount === 1 ? "product" : "products";
      if (!window.confirm(`Remove ${selectedCount} selected ${noun} from active inventory? Stock and history will be preserved.`)) {
        event.preventDefault();
      }
    });
    syncProductSelection();
  }

  document.querySelectorAll("form[method='post']").forEach((form) => {
    if (form.id === "assistant-form") return;
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      if (!form.checkValidity()) return;
      const button = form.querySelector("button[type='submit']");
      if (!button || button.dataset.loading === "true") return;
      button.dataset.loading = "true";
      button.dataset.label = button.textContent;
      button.textContent = "Working…";
      button.setAttribute("aria-busy", "true");
      button.classList.add("is-loading");
    });
  });
})();
