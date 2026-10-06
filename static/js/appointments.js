/* Appointments: small conveniences. Every page works without this file. */
(function () {
  "use strict";

  // Doctor filter: show the list as soon as a doctor is picked (the "Show" button still works).
  document.querySelectorAll("select[data-auto-submit]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.form) select.form.submit();
    });
  });

  // Booking form: "Patient is here now" means no date/time is needed, so hide those inputs.
  var walkIn = document.getElementById("id_walk_in");
  var scheduleFields = document.querySelector("[data-schedule-fields]");
  if (walkIn && scheduleFields) {
    var sync = function () { scheduleFields.hidden = walkIn.checked; };
    walkIn.addEventListener("change", sync);
    sync();
  }
})();
