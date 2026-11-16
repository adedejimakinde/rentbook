// Admin forms submit once: a second click or Enter while the first submit is
// in flight is ignored, and the submit buttons are disabled. The buttons are
// disabled after the browser has read the form, so the clicked button's name
// (for example "_continue") is still sent.
document.addEventListener("submit", function (event) {
  var form = event.target;
  // getAttribute, not form.method: a field named "method" would shadow it.
  if ((form.getAttribute("method") || "").toLowerCase() !== "post") {
    return;
  }
  if (form.dataset.submitted) {
    event.preventDefault();
    return;
  }
  form.dataset.submitted = "true";
  setTimeout(function () {
    form.querySelectorAll("button[type=submit], input[type=submit], button:not([type])").forEach(
      function (button) {
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
      }
    );
  }, 0);
});
// A page restored from the back/forward cache gets its buttons back.
window.addEventListener("pageshow", function (event) {
  if (!event.persisted) {
    return;
  }
  document.querySelectorAll("form[data-submitted]").forEach(function (form) {
    delete form.dataset.submitted;
    form.querySelectorAll("[aria-disabled=true]").forEach(function (button) {
      button.disabled = false;
      button.removeAttribute("aria-disabled");
    });
  });
});
