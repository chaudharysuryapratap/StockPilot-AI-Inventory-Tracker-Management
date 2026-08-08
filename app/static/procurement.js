(() => {
  "use strict";
  const form = document.getElementById("unit-conversion-form");
  const product = document.getElementById("conversion-product");
  if (!form || !product) return;
  form.addEventListener("submit", () => {
    form.action = `/products/${encodeURIComponent(product.value)}/unit-conversions`;
  });

  const manualProduct = document.getElementById("manual-po-product");
  const manualUnit = document.getElementById("manual-po-unit");
  if (manualProduct && manualUnit) {
    manualProduct.addEventListener("change", () => {
      const option = manualProduct.options[manualProduct.selectedIndex];
      manualUnit.value = option?.dataset.baseUnit || "";
    });
  }
})();
