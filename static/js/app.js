(function () {
  "use strict";

  // Icons (Lucide)
  if (window.lucide) { window.lucide.createIcons({ attrs: { class: "icon" } }); }

  // Mobile sidebar
  var sb = document.getElementById("sidebar");
  var toggle = document.getElementById("menu-toggle");
  if (sb && toggle) {
    toggle.addEventListener("click", function () { sb.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (sb.classList.contains("open") && !sb.contains(e.target) && !toggle.contains(e.target)) sb.classList.remove("open");
    });
  }

  // Confirmation dialog for destructive actions: <form data-confirm="..." data-confirm-title="...">
  var dlg = document.getElementById("confirm-dialog");
  var pending = null;
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f.dataset || !f.dataset.confirm || !dlg || f.dataset.confirmed === "1") return;
    e.preventDefault();
    pending = f;
    dlg.querySelector("[data-title]").textContent = f.dataset.confirmTitle || "Please confirm";
    dlg.querySelector("[data-msg]").textContent = f.dataset.confirm;
    var extra = dlg.querySelector("[data-extra]");
    extra.innerHTML = "";
    if (f.dataset.confirmCheck) {
      var l = document.createElement("label");
      l.style.cssText = "display:flex;gap:8px;align-items:flex-start;font-weight:500;margin-top:12px";
      var c = document.createElement("input");
      c.type = "checkbox"; c.style.width = "auto"; c.style.marginTop = "3px"; c.id = "dlg-check";
      l.appendChild(c); l.appendChild(document.createTextNode(f.dataset.confirmCheck));
      extra.appendChild(l);
    }
    dlg.showModal();
  });
  if (dlg) {
    dlg.querySelector("[data-cancel]").addEventListener("click", function () { dlg.close(); pending = null; });
    dlg.querySelector("[data-ok]").addEventListener("click", function () {
      if (!pending) return;
      var cb = dlg.querySelector("#dlg-check");
      if (pending.dataset.confirmCheck) {
        if (!cb.checked) { cb.focus(); return; }
        var h = pending.querySelector("input[name=with_history]");
        if (h) h.value = "yes";
      }
      pending.dataset.confirmed = "1";
      dlg.close();
      pending.submit();
    });
  }

  // Client-side validation (convenience only - the server re-validates everything)
  document.querySelectorAll("form[data-validate]").forEach(function (form) {
    form.setAttribute("novalidate", "novalidate");
    form.addEventListener("submit", function (e) {
      var bad = false;
      form.querySelectorAll("[data-err-for]").forEach(function (el) { if (!el.dataset.server) el.textContent = ""; });
      form.querySelectorAll("input, select, textarea").forEach(function (inp) {
        var wrap = inp.closest(".field");
        if (wrap) wrap.classList.remove("has-error");
        var msg = "";
        if (inp.required && !inp.value.trim()) msg = "This field is required.";
        else if (inp.type === "number" && inp.value !== "") {
          var v = parseFloat(inp.value);
          if (isNaN(v)) msg = "Enter a valid number.";
          else if (inp.min !== "" && v < parseFloat(inp.min)) msg = "Must be " + (inp.min === "0" ? "zero or more." : "at least " + inp.min + ".");
          else if (inp.dataset.positive && v <= 0) msg = "Must be greater than zero.";
        } else if (inp.type === "date" && inp.max && inp.value > inp.max) msg = "Date cannot be in the future.";
        if (msg) {
          bad = true;
          if (wrap) {
            wrap.classList.add("has-error");
            var slot = wrap.querySelector("[data-err-for]");
            if (slot) { slot.textContent = msg; slot.removeAttribute("data-server"); }
          }
        }
      });
      if (bad) {
        e.preventDefault();
        e.stopImmediatePropagation();
        var first = form.querySelector(".has-error input, .has-error select, .has-error textarea");
        if (first) first.focus();
      }
    }, true);
  });

  // Stock form: live availability hint (server still enforces the rule)
  var hint = document.getElementById("stock-hint");
  var qtyIn = document.getElementById("quantity");
  var itemSel = document.getElementById("item_id");
  var loanSel = document.getElementById("loan");
  if (hint && (itemSel || loanSel)) {
    var type = hint.dataset.type;
    var round3 = function (n) { return Math.round(n * 1000) / 1000; };
    var refresh = function () {
      hint.className = "stock-hint";
      var qv = qtyIn ? parseFloat(qtyIn.value) : NaN;
      if (type === "RETURN") {
        var lo = loanSel.options[loanSel.selectedIndex];
        if (!lo || !lo.value) { hint.textContent = "Select the open loan being returned."; return; }
        var held = parseFloat(lo.dataset.held), u = lo.dataset.unit;
        var t = "On loan: " + held + " " + u;
        if (!isNaN(qv) && qv > held) { t += " - more than this borrower has out; it will be rejected."; hint.classList.add("bad"); }
        else if (!isNaN(qv)) t += " - still out after return: " + round3(held - qv) + " " + u;
        hint.textContent = t; return;
      }
      var opt = itemSel.options[itemSel.selectedIndex];
      if (!opt || !opt.value) { hint.textContent = "Select an item to see the current stock."; return; }
      var avail = parseFloat(opt.dataset.qty), unit = opt.dataset.unit;
      var txt = "Currently in store: " + avail + " " + unit;
      if (type !== "IN" && !isNaN(qv)) {
        if (qv > avail) { txt += " - this exceeds what is in the store and will be rejected."; hint.classList.add("bad"); }
        else txt += " - left after " + (type === "LOAN" ? "lending" : "issue") + ": " + round3(avail - qv) + " " + unit;
      } else if (type === "IN" && !isNaN(qv)) {
        txt += " - after receipt: " + round3(avail + qv) + " " + unit;
      }
      hint.textContent = txt;
      if (type === "IN") {
        var sup = document.getElementById("counterparty"), pr = document.getElementById("unit_price");
        if (sup && !sup.dataset.touched && opt.dataset.supplier) sup.value = opt.dataset.supplier;
        if (pr && !pr.dataset.touched && opt.dataset.price) pr.value = opt.dataset.price;
      }
    };
    ["counterparty", "unit_price"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener("input", function () { el.dataset.touched = "1"; });
    });
    if (itemSel) itemSel.addEventListener("change", refresh);
    if (loanSel) loanSel.addEventListener("change", function () {
      var lo = loanSel.options[loanSel.selectedIndex];
      if (qtyIn && lo && lo.dataset.held && !qtyIn.value) qtyIn.value = String(parseFloat(lo.dataset.held));
      refresh();
    });
    if (qtyIn) qtyIn.addEventListener("input", refresh);
    refresh();
  }

  // Dashboard charts
  var dataEl = document.getElementById("dash-data");
  if (dataEl && window.Chart) {
    var d = JSON.parse(dataEl.textContent);
    var css = getComputedStyle(document.documentElement);
    var grid = "#e3d9c2", txt = "#6a5f4e";
    Chart.defaults.font.family = css.getPropertyValue("--sans");
    Chart.defaults.color = txt;
    new Chart(document.getElementById("chart-movement"), {
      type: "bar",
      data: {
        labels: d.weeks.map(function (w) { return w.label; }),
        datasets: [
          { label: "Stock in", data: d.weeks.map(function (w) { return w.in; }), backgroundColor: "#2e6b3b", borderRadius: 2 },
          { label: "Stock out", data: d.weeks.map(function (w) { return w.out; }), backgroundColor: "#bf7a1a", borderRadius: 2 }
        ]
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } } },
        scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: grid }, title: { display: true, text: "Units moved per week" } } }
      }
    });
    var palette = ["#2e6b3b", "#bf7a1a", "#35607f", "#8a5a35", "#7d9a3f", "#6f6a5c", "#ad412a", "#5b3f7a"];
    new Chart(document.getElementById("chart-category"), {
      type: "doughnut",
      data: {
        labels: d.categories.map(function (c) { return c.name; }),
        datasets: [{ data: d.categories.map(function (c) { return c.n; }), backgroundColor: palette, borderColor: "#fffcf4", borderWidth: 2 }]
      },
      options: {
        maintainAspectRatio: false, cutout: "62%",
        plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { return " " + c.label + ": " + c.parsed + " item" + (c.parsed === 1 ? "" : "s"); } } } }
      }
    });
  }

  // Print
  document.querySelectorAll("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  // Auto-submit filter selects
  document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });

  // Report form: show/hide inputs by report kind
  var kindSel = document.getElementById("report-kind");
  if (kindSel) {
    var sync = function () {
      var k = kindSel.value;
      var dated = ["inventory", "item", "stock_in", "stock_out", "usage"].indexOf(k) >= 0;
      document.querySelectorAll("[data-needs-dates]").forEach(function (el) { el.style.display = dated ? "" : "none"; });
      document.querySelectorAll("[data-needs-item]").forEach(function (el) { el.style.display = k === "item" ? "" : "none"; });
    };
    kindSel.addEventListener("change", sync);
    sync();
  }
})();
