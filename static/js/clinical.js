/* Clinical pages — small progressive enhancements. Every page still works without JavaScript. */
(function () {
  "use strict";

  // The visit form remembers whether anything was typed or changed, so that leaving the page
  // can ask first instead of silently throwing the doctor's notes away (see the end of this file).
  var visitForm = document.querySelector("[data-visit-form]");
  var formChanged = false;
  function markChanged() { formChanged = true; }

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
      markChanged();
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

  // The "Visit date" box: an earlier day when a visit is typed in from a paper file.
  var visitDateInput = document.querySelector("input[data-visit-date]");

  document.querySelectorAll("[data-follow-up]").forEach(function (box) {
    var savedBase = parseIsoDate(box.getAttribute("data-base-date")) || new Date();
    var dateInput = box.querySelector('input[type="date"]');
    var picks = box.querySelectorAll('input[type="radio"]');
    if (!dateInput) return;

    // Quick picks count from the visit date, as the server does (apps/clinical/forms.py visit_day).
    function baseDate() {
      return (visitDateInput && parseIsoDate(visitDateInput.value)) || savedBase;
    }
    function fillFromPick() {
      picks.forEach(function (radio) {
        if (radio.checked) dateInput.value = toIsoDate(addInterval(baseDate(), radio.value));
      });
    }
    picks.forEach(function (radio) { radio.addEventListener("change", fillFromPick); });
    // A different visit date moves a picked follow-up with it ("2 weeks" after the new date).
    if (visitDateInput) visitDateInput.addEventListener("change", fillFromPick);
    // Typing a date by hand clears the quick pick, so the typed date is the one saved.
    dateInput.addEventListener("input", function () {
      picks.forEach(function (radio) { radio.checked = false; });
    });
  });

  // --- Visit form: don't silently throw away typed notes ------------------------------
  if (visitForm) {
    visitForm.addEventListener("input", markChanged);
    visitForm.addEventListener("change", markChanged);  // date pickers, quick picks, "Remove" boxes

    // Registered on the document after app.js, so a form another script stopped is left alone.
    // (A second tap on Save is ignored by app.js, and the server's one-time form_token makes
    // a repeated save open the visit already saved.)
    document.addEventListener("submit", function (e) {
      if (e.target !== visitForm || e.defaultPrevented) return;
      formChanged = false;  // saving is not leaving
    });

    // Any other way off the page (a link, the back button, closing the tab) asks first.
    window.addEventListener("beforeunload", function (e) {
      if (!formChanged) return;
      e.preventDefault();
      e.returnValue = "";  // needed by some browsers to show their "Leave site?" prompt
    });
  }

  // "Repeat last prescription" reloads the page: ask in our own words, then don't ask twice.
  document.addEventListener("click", function (e) {
    var link = e.target.closest("[data-repeat-rx]");
    if (!link) return;
    if (formChanged && !window.confirm("Start again with the last prescription? What you have typed on this page will be lost.")) {
      e.preventDefault();
      return;
    }
    formChanged = false;
  });

  // --- Printable prescription --------------------------------------------------------
  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () { window.print(); });
  });
  // Paper size / letterhead: show the result straight away (without JavaScript, "Apply" does it).
  document.querySelectorAll("[data-print-options]").forEach(function (form) {
    var apply = form.querySelector("[data-print-options-apply]");
    if (apply) apply.hidden = true;
    form.addEventListener("change", function () { form.submit(); });
  });
})();
