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
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    append("assistant-question", question);
    input.value = "";
    input.disabled = true;
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
      append("assistant-answer", payload.answer);
    } catch (error) {
      append("assistant-error", error.message || "Assistant unavailable");
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
})();
