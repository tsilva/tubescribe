// Fetch submission also supports browsers that do not navigate native POST forms.
const form = document.querySelector("form[data-artifact]");
if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const error = document.querySelector("[data-artifact-error]");
    button.disabled = true;
    error.textContent = "";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || "Could not start the workflow");
      }
      window.location.assign(response.url);
    } catch (failure) {
      error.textContent = failure.message;
      button.disabled = false;
    }
  });
}
