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
  if (manualProduct && manualUnit) {
    manualProduct.addEventListener("change", () => {
      const option = manualProduct.options[manualProduct.selectedIndex];
      manualUnit.value = option?.dataset.baseUnit || "";
    });
  }

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
