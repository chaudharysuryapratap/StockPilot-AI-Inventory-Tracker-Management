(() => {
  "use strict";

  document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.passwordToggle);
      if (!input) return;
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.textContent = showing ? "Show" : "Hide";
      button.setAttribute("aria-label", `${showing ? "Show" : "Hide"} password`);
    });
  });

  function username(value) {
    return value
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 63);
  }

  const businessName = document.getElementById("business-name");
  const businessUsername = document.getElementById("business-username");
  const usernameStatus = document.getElementById("business-username-status");
  const signupForm = document.querySelector(".signup-form");
  let usernameWasEdited = false;
  let checkTimer = null;

  async function checkAvailability() {
    if (!businessUsername || !usernameStatus || !signupForm) return;
    const value = username(businessUsername.value);
    businessUsername.value = value;
    if (value.length < 3) {
      usernameStatus.textContent = "Use at least 3 lowercase letters, numbers, or hyphens.";
      usernameStatus.className = "field-status error";
      return;
    }
    usernameStatus.textContent = "Checking availability…";
    usernameStatus.className = "field-status checking";
    try {
      const response = await fetch(
        `${signupForm.dataset.usernameAvailability}?username=${encodeURIComponent(value)}`,
        { headers: { Accept: "application/json" }, cache: "no-store" },
      );
      const result = await response.json();
      usernameStatus.textContent = result.available
        ? `@${result.username} is available.`
        : `@${result.username} is already in use.`;
      usernameStatus.className = `field-status ${result.available ? "success" : "error"}`;
      businessUsername.setCustomValidity(result.available ? "" : "Business username is already in use");
    } catch (_) {
      usernameStatus.textContent = "Availability will be checked when you submit.";
      usernameStatus.className = "field-status";
      businessUsername.setCustomValidity("");
    }
  }

  businessName?.addEventListener("input", () => {
    if (!businessUsername || usernameWasEdited) return;
    businessUsername.value = username(businessName.value);
    clearTimeout(checkTimer);
    checkTimer = setTimeout(checkAvailability, 350);
  });
  businessUsername?.addEventListener("input", () => {
    usernameWasEdited = true;
    businessUsername.value = username(businessUsername.value);
    businessUsername.setCustomValidity("");
    clearTimeout(checkTimer);
    checkTimer = setTimeout(checkAvailability, 350);
  });

  const loginBusiness = document.getElementById("login-business-username");
  const ssoLink = document.getElementById("sso-login-link");
  function updateSsoLink() {
    if (!loginBusiness || !ssoLink) return;
    const value = username(loginBusiness.value);
    if (value.length >= 3) {
      ssoLink.href = `/auth/sso/${encodeURIComponent(value)}`;
      ssoLink.classList.remove("disabled");
      ssoLink.setAttribute("aria-disabled", "false");
    } else {
      ssoLink.href = "#";
      ssoLink.classList.add("disabled");
      ssoLink.setAttribute("aria-disabled", "true");
    }
  }
  loginBusiness?.addEventListener("input", updateSsoLink);
  ssoLink?.addEventListener("click", (event) => {
    if (ssoLink.classList.contains("disabled")) {
      event.preventDefault();
      loginBusiness?.focus();
    }
  });
  updateSsoLink();
})();
