/* Service history (BMW / MINI iDrive) — standalone page.
   Deliberately self-contained: it shares styles.css tokens and nothing else,
   so it can't collide with app.js. All the domain knowledge lives in the
   backend (backend/profiles/bmw/service_history.py) — this file is a form. */

(function () {
  "use strict";

  var BRAND = "bmw";
  var catalogue = null;
  var showingAll = false;
  var lastArgument = "";
  var lastBody = null;

  var $ = function (id) { return document.getElementById(id); };

  function fail(msg) {
    var el = $("sh-err");
    el.textContent = msg;
    el.classList.add("show");
  }
  function clearFail() { $("sh-err").classList.remove("show"); }

  /* ---------------------------------------------------------------- load */

  fetch("/api/" + BRAND + "/service-history")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      catalogue = data;
      renderCatalogue();
    })
    .catch(function (err) {
      $("sh-lead").textContent =
        "Couldn't load the catalogue — is the backend running? (" + err.message + ")";
      $("sh-build").disabled = true;
    });

  function renderCatalogue() {
    $("sh-lead").textContent =
      "Head unit " + catalogue.huAddress + ", job " + catalogue.job +
      ", up to " + catalogue.maxItems + " items per visit.";

    var status = $("sh-status");
    status.innerHTML = "";
    catalogue.statuses.forEach(function (s) {
      var o = document.createElement("option");
      o.value = s.value;
      o.textContent = s.label;
      if (s.value === 1) o.selected = true;
      status.appendChild(o);
    });

    $("sh-dealer").maxLength = catalogue.dealerNameMax;

    var tbody = $("sh-hu");
    tbody.innerHTML = "";
    catalogue.headUnits.forEach(function (hu) {
      var tr = document.createElement("tr");
      var name = document.createElement("td");
      name.textContent = hu.label;
      var prg = document.createElement("td");
      prg.className = "prg";
      prg.textContent = hu.prg;
      var sup = document.createElement("td");
      var says = { yes: "confirmed working", unconfirmed: "unconfirmed",
                   unlikely: "unlikely to hold history" }[hu.history] || "unknown";
      sup.textContent = hu.note ? says + " — " + hu.note : says;
      tr.appendChild(name); tr.appendChild(prg); tr.appendChild(sup);
      tbody.appendChild(tr);
    });

    renderItems();

    var today = new Date();
    $("sh-date").value =
      today.getFullYear() + "-" +
      String(today.getMonth() + 1).padStart(2, "0") + "-" +
      String(today.getDate()).padStart(2, "0");
  }

  function renderItems() {
    var host = $("sh-items");
    var checked = {};
    Array.prototype.forEach.call(
      host.querySelectorAll("input[type=checkbox]"), function (cb) {
        if (cb.checked) checked[cb.value] = true;
      });

    host.innerHTML = "";
    catalogue.items
      .filter(function (i) { return showingAll || i.common; })
      .forEach(function (item) {
        var label = document.createElement("label");
        label.className = "sh-item";
        if (checked[item.code]) label.classList.add("on");
        if (item.note) label.title = item.note;

        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = item.code;
        cb.checked = !!checked[item.code];
        cb.addEventListener("change", function () {
          label.classList.toggle("on", cb.checked);
        });

        var name = document.createElement("span");
        name.className = "sh-name";
        name.textContent = item.name;

        var code = document.createElement("span");
        code.className = "sh-code";
        code.textContent = item.code;

        label.appendChild(cb); label.appendChild(name); label.appendChild(code);
        host.appendChild(label);
      });

    $("sh-toggle-all").textContent =
      showingAll ? "Show the common items only" : "Show all codes";
  }

  $("sh-toggle-all").addEventListener("click", function () {
    showingAll = !showingAll;
    renderItems();
  });

  /* --------------------------------------------------------- dealer name */

  $("sh-dealer").addEventListener("input", function () {
    var v = $("sh-dealer").value;
    var hint = $("sh-dealer-hint");
    var max = (catalogue && catalogue.dealerNameMax) || 7;
    hint.textContent = v.length + " of " + max + " characters.";
    hint.classList.toggle("bad", v.length === 0);
  });

  /* --------------------------------------------------------------- build */

  $("sh-build").addEventListener("click", function () {
    clearFail();
    if (!catalogue) return;

    var dateStr = $("sh-date").value;
    if (!dateStr) { fail("Pick the date the car was serviced."); return; }
    var parts = dateStr.split("-");

    var items = [];
    Array.prototype.forEach.call(
      $("sh-items").querySelectorAll("input[type=checkbox]"), function (cb) {
        if (cb.checked) {
          items.push({
            code: parseInt(cb.value, 10),
            status: parseInt($("sh-status").value, 10),
            distance_km: 0,
            months: 0
          });
        }
      });
    if (!items.length) { fail("Tick at least one thing you did to the car."); return; }

    var body = {
      day: parseInt(parts[2], 10),
      month: parseInt(parts[1], 10),
      year: parseInt(parts[0], 10),
      odometer: parseFloat($("sh-odo").value || "0"),
      units: $("sh-units").value,
      dealer_name: $("sh-dealer").value.trim().toUpperCase(),
      bmw_dealer: $("sh-who").value === "1",
      status: parseInt($("sh-status").value, 10),
      items: items,
      pad: true
    };

    fetch("/api/" + BRAND + "/service-history/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function (result) {
        lastBody = body;
        showResult(result);
      })
      .catch(function (err) { fail(err.message); });
  });

  /* --------------------------------------------------------------- write */

  $("sh-write").addEventListener("click", function () {
    clearFail();
    $("sh-ok").classList.remove("show");
    if (!catalogue || !lastBody) return;

    var s = lastBody;
    var what =
      "Write this entry to the car's service history?\n\n" +
      s.day + "/" + s.month + "/" + s.year + " · " + s.odometer + " " + s.units +
      " · " + (s.dealer_name || "(no dealer name)") + " · " + s.items.length + " item(s)\n\n" +
      "This cannot be edited or deleted afterwards. The head unit reboots so the entry shows.";
    if (!window.confirm(what)) return;

    var btn = $("sh-write");
    btn.disabled = true;
    btn.textContent = "Writing…";

    fetch("/api/" + BRAND + "/service-history/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({}, lastBody, { confirm: true, reboot: true }))
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function (result) {
        var el = $("sh-ok");
        el.textContent = result.message || "Written.";
        el.classList.add("show");
      })
      .catch(function (err) { fail(err.message); })
      .then(function () {
        btn.disabled = false;
        btn.textContent = "Write to the car";
      });
  });

  function showResult(result) {
    lastArgument = result.argument;
    $("sh-arg").textContent = result.argument;

    var chips = $("sh-chips");
    chips.innerHTML = "";
    var s = result.summary;
    [
      ["Date", s.date],
      ["Odometer", s.odometerKm.toLocaleString() + " km (" + s.odometerEntered + ")"],
      ["Dealer", s.dealer + (s.bmwDealer ? " · BMW" : " · independent")],
      ["Status", s.status],
      ["Fields", result.fieldCount + (result.padded ? " (padded)" : " (short form)")]
    ].forEach(function (pair) {
      var c = document.createElement("span");
      c.className = "sh-chip";
      c.innerHTML = pair[0] + " <b></b>";
      c.querySelector("b").textContent = pair[1];
      chips.appendChild(c);
    });
    s.items.forEach(function (item) {
      var c = document.createElement("span");
      c.className = "sh-chip";
      c.innerHTML = "<b></b>";
      c.querySelector("b").textContent = item.name;
      chips.appendChild(c);
    });

    var notes = $("sh-notes");
    notes.innerHTML = "";
    result.notes.forEach(function (n) {
      var li = document.createElement("li");
      li.textContent = n;
      notes.appendChild(li);
    });

    $("sh-out").classList.add("show");
    $("sh-copy").disabled = false;
    $("sh-write").disabled = false;
    $("sh-out").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* ---------------------------------------------------------------- copy */

  $("sh-copy").addEventListener("click", function () {
    if (!lastArgument) return;
    var btn = $("sh-copy");
    var done = function () {
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = "Copy argument"; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(lastArgument).then(done, fallback);
    } else {
      fallback();
    }
    function fallback() {
      // No clipboard API (or it refused) — select it so Ctrl+C works.
      var range = document.createRange();
      range.selectNodeContents($("sh-arg"));
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      btn.textContent = "Selected — press Ctrl+C";
      setTimeout(function () { btn.textContent = "Copy argument"; }, 2200);
    }
  });
})();
