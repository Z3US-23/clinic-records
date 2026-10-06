/* Clinic Records — small progressive enhancements. Every page must still work without JS. */
(function () {
  "use strict";

  // --- Mobile sidebar -------------------------------------------------------
  var sidebar = document.getElementById("sidebar");
  var backdrop = document.querySelector("[data-sidebar-backdrop]");
  function setSidebar(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (backdrop) backdrop.hidden = !open;
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-toggle-sidebar]")) setSidebar(!sidebar.classList.contains("open"));
    else if (e.target.closest("[data-sidebar-backdrop]")) setSidebar(false);
  });

  // --- Confirm before destructive actions: <form data-confirm="Cancel this appointment?"> ---
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var submitter = e.submitter;
    var message = (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) e.preventDefault();
  });

  // --- Flash messages -------------------------------------------------------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-dismiss]");
    if (btn) btn.closest(".alert").remove();
  });
  setTimeout(function () {
    document.querySelectorAll("[data-auto-dismiss].alert-success, [data-auto-dismiss].alert-info").forEach(function (el) {
      el.style.transition = "opacity .3s";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 300);
    });
  }, 6000);

  // --- Live patient search (topbar and any [data-patient-search]) ----------
  // Expects JSON: {"results": [{"id", "name", "mrn", "phone", "age_sex", "url"}]}
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  document.querySelectorAll("[data-patient-search]").forEach(function (box) {
    var input = box.querySelector("input");
    var panel = box.querySelector(".search-results");
    var url = box.getAttribute("data-url");
    var timer = null, active = -1, lastQuery = "", controller = null;
    if (!input || !panel || !url) return;

    function items() { return panel.querySelectorAll(".search-result"); }
    function highlight(i) {
      var list = items();
      list.forEach(function (el, idx) { el.classList.toggle("is-active", idx === i); });
      active = i;
      if (list[i]) list[i].scrollIntoView({ block: "nearest" });
    }
    function close() { panel.hidden = true; active = -1; }

    function render(results, q) {
      if (!results.length) {
        panel.innerHTML = '<div class="search-empty">No patient found for “' + escapeHtml(q) + '”.</div>';
      } else {
        panel.innerHTML = results.map(function (p) {
          return '<a class="search-result" role="option" href="' + escapeHtml(p.url) + '">' +
            '<span class="avatar avatar-sm">' + escapeHtml((p.name || "?").split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join("").toUpperCase()) + "</span>" +
            "<span><strong>" + escapeHtml(p.name) + '</strong><br><span class="meta">' +
            escapeHtml([p.mrn, p.age_sex, p.phone].filter(Boolean).join(" · ")) + "</span></span></a>";
        }).join("");
      }
      panel.hidden = false;
      active = -1;
    }

    function search(q) {
      if (controller) controller.abort();
      controller = "AbortController" in window ? new AbortController() : null;
      fetch(url + "?q=" + encodeURIComponent(q), {
        headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
        credentials: "same-origin",
        signal: controller ? controller.signal : undefined,
      })
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (data) { if (q === lastQuery) render(data.results || [], q); })
        .catch(function () {});
    }

    input.addEventListener("input", function () {
      var q = input.value.trim();
      lastQuery = q;
      clearTimeout(timer);
      if (q.length < 2) { close(); return; }
      timer = setTimeout(function () { search(q); }, 180);
    });
    input.addEventListener("keydown", function (e) {
      var list = items();
      if (e.key === "ArrowDown" && list.length) { e.preventDefault(); highlight(Math.min(active + 1, list.length - 1)); }
      else if (e.key === "ArrowUp" && list.length) { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (e.key === "Enter") {
        if (active >= 0 && list[active]) { e.preventDefault(); window.location = list[active].href; }
        else if (list.length === 1) { e.preventDefault(); window.location = list[0].href; }
      } else if (e.key === "Escape") { close(); input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) close(); });
    input.addEventListener("focus", function () { if (panel.innerHTML && input.value.trim().length >= 2) panel.hidden = false; });
  });

  // Keyboard shortcut: "/" focuses the patient search.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey) return;
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
    var input = document.querySelector(".topbar [data-patient-search] input");
    if (input) { e.preventDefault(); input.focus(); }
  });

  // --- Installable app (PWA) -----------------------------------------------
  var swUrl = document.body.getAttribute("data-sw-url");
  if (swUrl && "serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(swUrl, { scope: "/" }).catch(function () {});
    });
  }
})();
