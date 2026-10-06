/* Reminders pages — small progressive enhancements. Everything works without JS:
   the send buttons then simply open WhatsApp in the same tab. */
(function () {
  "use strict";

  // --- One-tap send ---------------------------------------------------------
  // Forms (or submit buttons) marked data-wa-send open WhatsApp in a new tab.
  // This page then refreshes (or goes to data-after-url) so the reminder shows as sent.
  var REFRESH_DELAY_MS = 1500;

  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return; // e.g. the user said "No" to a confirm question
    var form = e.target;
    var submitter = e.submitter;
    var isSend = form.hasAttribute("data-wa-send") || (submitter && submitter.hasAttribute("data-wa-send"));
    if (!isSend) return;

    form.setAttribute("target", "_blank");
    // Only this submission goes to the new tab; a later "Save" stays in this tab.
    setTimeout(function () { form.removeAttribute("target"); }, 0);

    var afterUrl = (submitter && submitter.getAttribute("data-after-url")) || form.getAttribute("data-after-url");
    setTimeout(function () {
      if (afterUrl) window.location.assign(afterUrl);
      else window.location.reload();
    }, REFRESH_DELAY_MS);
  });

  // --- "How sending works" tip: remember when it was hidden ----------------
  var HELP_KEY = "clinic-records.reminders.help-hidden";
  var help = document.querySelector("[data-reminders-help]");
  if (help) {
    try {
      if (window.localStorage.getItem(HELP_KEY) === "1") help.remove();
    } catch (err) { /* storage blocked: just show the tip */ }
    help.addEventListener("click", function (e) {
      if (!e.target.closest("[data-dismiss]")) return;
      try { window.localStorage.setItem(HELP_KEY, "1"); } catch (err) { /* ignore */ }
    });
  }

  // --- Live preview of a message as it is typed -----------------------------
  // <textarea data-live-preview="element-id" data-kind="appointment">; sample values
  // for templates come from <script id="template-samples" type="application/json">.
  // Same rule as the server: only {plain_name} placeholders we know are filled in.
  var samples = {};
  var samplesEl = document.getElementById("template-samples");
  if (samplesEl) {
    try { samples = JSON.parse(samplesEl.textContent) || {}; } catch (err) { samples = {}; }
  }
  function fill(body, values) {
    return body.replace(/\{([a-z_]+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match;
    });
  }
  document.querySelectorAll("textarea[data-live-preview]").forEach(function (area) {
    var target = document.getElementById(area.getAttribute("data-live-preview"));
    if (!target) return;
    var values = samples[area.getAttribute("data-kind")] || {};
    area.addEventListener("input", function () {
      target.textContent = fill(area.value, values); // textContent: never parsed as HTML
    });
  });

  // The server-side "Preview" button is only needed without JS.
  document.querySelectorAll("[data-no-js-only]").forEach(function (el) { el.hidden = true; });

  // --- Character counter ----------------------------------------------------
  document.querySelectorAll("textarea[data-char-count]").forEach(function (area) {
    var max = parseInt(area.getAttribute("data-char-count"), 10);
    if (!max) return;
    var counter = document.createElement("span");
    counter.className = "rm-count";
    counter.setAttribute("aria-live", "polite");
    area.insertAdjacentElement("afterend", counter);
    function update() {
      counter.textContent = area.value.length + " / " + max + " characters";
      counter.classList.toggle("is-over", area.value.length > max);
    }
    area.addEventListener("input", update);
    update();
  });
})();
