/* Clinical pages — small progressive enhancements. Every page still works without JavaScript. */
(function () {
  "use strict";

  // --- Prescription rows: "Add medicine" -------------------------------------------
  // Copies the formset's empty row (<template data-formset-template>) and bumps TOTAL_FORMS.
  document.querySelectorAll("[data-formset]").forEach(function (box) {
    var prefix = box.getAttribute("data-prefix");
    var rows = box.querySelector("[data-formset-rows]");
    var template = box.querySelector("template[data-formset-template]");
    var addButton = box.querySelector("[data-formset-add]");
    var total = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
    var max = document.getElementById("id_" + prefix + "-MAX_NUM_FORMS");
    if (!rows || !template || !addButton || !total) return;

    function atLimit() {
      return max && parseInt(total.value, 10) >= parseInt(max.value, 10);
    }

    addButton.hidden = false;
    addButton.addEventListener("click", function () {
      if (atLimit()) return;
      var index = parseInt(total.value, 10);
      // The template is server-rendered (already escaped); only the row number is swapped in.
      var holder = document.createElement("div");
      holder.innerHTML = template.innerHTML.replace(/__prefix__/g, String(index)).trim();
      var row = holder.firstElementChild;
      rows.appendChild(row);
      total.value = index + 1;
      addButton.disabled = atLimit();
      var firstInput = row.querySelector("input:not([type=hidden])");
      if (firstInput) firstInput.focus();
    });
  });

  // --- Follow-up quick picks ("1 week", "1 month"...) fill the date box ----------------
  function parseIsoDate(text) {
    var parts = (text || "").split("-").map(Number);
    if (parts.length !== 3 || parts.some(isNaN)) return null;
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }
  function toIsoDate(d) {
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  // Same rules as apps/clinical/utils.py follow_up_from(): "7d" = 7 days, "1m" = 1 month
  // (31 Jan + 1 month = 28/29 Feb).
  function addInterval(base, choice) {
    var amount = parseInt(choice, 10);
    var unit = choice.slice(-1);
    if (unit === "d") {
      return new Date(base.getFullYear(), base.getMonth(), base.getDate() + amount);
    }
    var target = new Date(base.getFullYear(), base.getMonth() + amount, 1);
    var lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
    target.setDate(Math.min(base.getDate(), lastDay));
    return target;
  }

  document.querySelectorAll("[data-follow-up]").forEach(function (box) {
    var base = parseIsoDate(box.getAttribute("data-base-date")) || new Date();
    var dateInput = box.querySelector('input[type="date"]');
    var picks = box.querySelectorAll('input[type="radio"]');
    if (!dateInput) return;

    picks.forEach(function (radio) {
      radio.addEventListener("change", function () {
        if (radio.checked) dateInput.value = toIsoDate(addInterval(base, radio.value));
      });
    });
    // Typing a date by hand clears the quick pick, so the typed date is the one saved.
    dateInput.addEventListener("input", function () {
      picks.forEach(function (radio) { radio.checked = false; });
    });
  });

  // --- "Repeat last prescription": don't silently throw away typed notes --------------
  var visitForm = document.querySelector("[data-visit-form]");
  var formChanged = false;
  if (visitForm) {
    visitForm.addEventListener("input", function () { formChanged = true; });
    visitForm.addEventListener("submit", function () { formChanged = false; });
  }
  document.addEventListener("click", function (e) {
    var link = e.target.closest("[data-repeat-rx]");
    if (link && formChanged && !window.confirm("Start again with the last prescription? What you have typed on this page will be lost.")) {
      e.preventDefault();
    }
  });

  // --- Printable prescription --------------------------------------------------------
  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () { window.print(); });
  });
})();
