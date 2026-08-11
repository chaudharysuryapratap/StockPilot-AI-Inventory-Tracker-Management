(() => {
  "use strict";
  const form = document.getElementById("unit-conversion-form");
  const product = document.getElementById("conversion-product");
  if (form && product) {
    form.addEventListener("submit", () => {
      form.action = `/products/${encodeURIComponent(product.value)}/unit-conversions`;
    });
  }

  const manualProduct = document.getElementById("manual-po-product");
  const manualUnit = document.getElementById("manual-po-unit");
  const manualCost = document.getElementById("manual-po-cost");
  const manualSupplier = document.getElementById("manual-po-supplier");
  const productHelp = document.getElementById("manual-po-product-help");
  if (manualProduct && manualUnit) {
    const applyProductDefaults = () => {
      const option = manualProduct.options[manualProduct.selectedIndex];
      manualUnit.value = option?.dataset.baseUnit || "";
      if (manualCost) manualCost.value = option?.dataset.unitCost || "0";
      if (manualSupplier && option?.dataset.supplierId) {
        manualSupplier.value = option.dataset.supplierId;
      }
      if (productHelp) {
        productHelp.textContent = !option?.value
          ? "Choose a product to prefill its purchasing defaults."
          : option.dataset.perishable === "true"
            ? "Perishable product: lot number and expiry date will be required when the goods are received."
            : "Standard product: batch and expiry details remain optional when the goods are received.";
      }
    };
    manualProduct.addEventListener("change", applyProductDefaults);
  }

  document.querySelectorAll(".po-receipt-form").forEach((receiptForm) => {
    const manufactured = receiptForm.querySelector("[name='manufactured_at']");
    const expiry = receiptForm.querySelector("[name='expiry_date']");
    if (!manufactured || !expiry) return;
    const validateDates = () => {
      expiry.min = manufactured.value || "";
      const invalid = manufactured.value && expiry.value && manufactured.value > expiry.value;
      expiry.setCustomValidity(invalid ? "Expiry date cannot be before the manufacturing date." : "");
    };
    manufactured.addEventListener("change", validateDates);
    expiry.addEventListener("change", validateDates);
  });

  const search = document.getElementById("po-search");
  const resultCount = document.getElementById("po-results");
  const cards = Array.from(document.querySelectorAll(".po-card"));
  let activeStatus = "all";

  function filterOrders() {
    const query = (search?.value || "").trim().toLowerCase();
    let visible = 0;
    cards.forEach((card) => {
      const matchesQuery = !query || (card.dataset.search || card.textContent).toLowerCase().includes(query);
      const matchesStatus = activeStatus === "all" || card.dataset.status === activeStatus;
      card.hidden = !(matchesQuery && matchesStatus);
      if (!card.hidden) visible += 1;
    });
    if (resultCount) resultCount.textContent = `${visible} of ${cards.length} orders`;
  }

  search?.addEventListener("input", filterOrders);
  document.querySelectorAll("[data-po-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeStatus = button.dataset.poFilter;
      document.querySelectorAll("[data-po-filter]").forEach((item) => item.classList.toggle("active", item === button));
      filterOrders();
    });
  });
  filterOrders();
})();
