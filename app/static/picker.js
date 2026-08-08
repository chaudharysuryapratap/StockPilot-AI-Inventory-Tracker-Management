(() => {
  "use strict";

  const status = document.getElementById("network-status");
  if (!status) return;

  function updateNetworkState() {
    const online = navigator.onLine;
    status.textContent = online ? "Online · actions enabled" : "Offline · read-only shell";
    status.className = `network-status ${online ? "online" : "offline"}`;
    document.querySelectorAll(".picker-order-card button[type='submit']").forEach((button) => {
      button.disabled = !online;
    });
  }

  updateNetworkState();
  window.addEventListener("online", updateNetworkState);
  window.addEventListener("offline", updateNetworkState);

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js").catch(() => {});
    });
  }
})();
