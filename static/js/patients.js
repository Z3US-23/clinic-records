/* Patients area — small extras. Every page works without this file. */
(function () {
  "use strict";

  // Patient list: re-run the search as soon as the sort order is changed.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.form) select.form.submit();
    });
  });
})();
