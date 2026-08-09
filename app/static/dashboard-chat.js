(() => {
  "use strict";
  const panel = document.getElementById("inventory-assistant");
  const form = document.getElementById("assistant-form");
  const input = document.getElementById("assistant-question");
  const messages = document.getElementById("assistant-messages");
  if (!panel || !form || !input || !messages) return;
  let conversationId = null;

  function append(className, text) {
    const paragraph = document.createElement("p");
    paragraph.className = className;
    paragraph.textContent = text;
    messages.appendChild(paragraph);
    messages.scrollTop = messages.scrollHeight;
    return paragraph;
  }

  document.querySelectorAll("[data-assistant-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      input.value = button.dataset.assistantPrompt;
      input.focus();
    });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    append("assistant-question", question);
    input.value = "";
    input.disabled = true;
    const submit = form.querySelector("button[type='submit']");
    const loading = append("assistant-loading", "");
    if (submit) {
      submit.disabled = true;
      submit.textContent = "Thinking…";
    }
    try {
      const response = await fetch(panel.dataset.chatUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": panel.dataset.csrfToken,
        },
        body: JSON.stringify({ question, conversation_id: conversationId }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Assistant unavailable");
      conversationId = payload.conversation_id;
      loading.remove();
      append("assistant-answer", payload.answer);
    } catch (error) {
      loading.remove();
      append("assistant-error", error.message || "Assistant unavailable");
    } finally {
      input.disabled = false;
      if (submit) {
        submit.disabled = false;
        submit.textContent = "Ask StockPilot";
      }
      input.focus();
    }
  });
})();
