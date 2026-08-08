(() => {
  "use strict";

  const status = document.getElementById("system-status");
  if (!status) return;

  fetch(status.dataset.healthUrl, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    cache: "no-store",
  })
    .then((response) => {
      if (!response.ok) throw new Error("unavailable");
      return response.json();
    })
    .then((payload) => {
      if (payload.status !== "ok" || payload.database !== "ok") {
        throw new Error("unavailable");
      }
      status.lastChild.textContent = " System online";
      status.classList.remove("error");
    })
    .catch(() => {
      status.lastChild.textContent = " System unavailable";
      status.classList.add("error");
    });
})();
