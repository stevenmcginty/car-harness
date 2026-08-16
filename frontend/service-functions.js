/* Service functions (BMW / MINI) — standalone page.
   Same rule as service-history.js: self-contained, shares only styles.css
   tokens, and holds no domain knowledge. What a CBS item means, which module
   owns it and which argument vector its cluster wants all live in
   backend/profiles/bmw/{cbs,battery}.py — this file asks and renders. */

(function () {
  "use strict";

  var BRAND = "bmw";
  var $ = function (id) { return document.getElementById(id); };

  function mode() { return $("sf-sim").checked ? "sim" : "real"; }

  function say(id, text, kind) {
    var box = $(id);
    box.textContent = text;
    box.className = "sf-msg show" + (kind ? " " + kind : "");
  }
  function hide(id) { $(id).className = "sf-msg"; }

  function busy(btn, on, label) {
    btn.disabled = on;
    if (on) {
      btn.dataset.label = btn.textContent;
      btn.textContent = label || "Working…";
    } else if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
    }
  }

  function api(path, options) {
    return fetch(path, options).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) {
          throw new Error(body && body.detail ? body.detail : "HTTP " + r.status);
        }
        return body;
      }, function () {
        throw new Error("HTTP " + r.status);
      });
    });
  }

  /* The one honesty rule this page enforces itself: a simulated answer is
     never allowed to look like a real one. */
  function tone(body) { return body.simulated ? "warn" : (body.ok ? "ok" : "bad"); }
  function label(body) {
    var text = body.message || "";
    /* The backend already leads simulated answers with "SIMULATED"; only add
       the mark when it hasn't, so the badge never reads twice. */
    if (body.simulated && text.toUpperCase().indexOf("SIMULATED") !== 0) {
      text = "SIMULATED — " + text;
    }
    return text;
  }

  $("sf-sim").addEventListener("change", function () {
    $("sf-simbanner").classList.toggle("show", $("sf-sim").checked);
  });

  /* ------------------------------------------------------------- CBS --- */

  var cbsCatalogue = null;

  api("/api/" + BRAND + "/cbs")
    .then(function (data) {
      cbsCatalogue = data;
      if (!data.available) {
        say("sf-cbs-msg", data.detail, "warn");
      }
    })
    .catch(function (err) {
      say("sf-cbs-msg", "Couldn't load the service-item catalogue: " + err.message, "bad");
    });

  function pct(value) {
    var n = parseFloat(value);
    return isNaN(n) ? null : Math.max(0, Math.min(100, n));
  }

  function renderCbs(body) {
    var tbody = $("sf-cbs-body");
    tbody.textContent = "";
    (body.items || []).forEach(function (item) {
      var tr = document.createElement("tr");

      var name = document.createElement("td");
      name.className = "name";
      name.textContent = item.label || item.kennung || "—";
      tr.appendChild(name);

      var kennung = document.createElement("td");
      kennung.className = "mono";
      kennung.textContent = item.kennung || "";
      tr.appendChild(kennung);

      var barCell = document.createElement("td");
      var value = pct(item.percent);
      if (value === null) {
        barCell.textContent = "—";
      } else {
        var bar = document.createElement("span");
        bar.className = "sf-bar" + (value <= 10 ? " out" : value <= 25 ? " low" : "");
        var fill = document.createElement("i");
        fill.style.width = value + "%";
        bar.appendChild(fill);
        barCell.appendChild(bar);
        var text = document.createElement("span");
        text.style.marginLeft = "9px";
        text.textContent = value + "%";
        barCell.appendChild(text);
      }
      tr.appendChild(barCell);

      var distance = document.createElement("td");
      distance.textContent = item.remaining
        ? item.remaining + " " + (item.remainingUnit || "")
        : "—";
      tr.appendChild(distance);

      var counter = document.createElement("td");
      counter.textContent = item.counter === undefined || item.counter === null
        ? "—" : item.counter;
      tr.appendChild(counter);

      var module = document.createElement("td");
      module.className = "mono";
      module.textContent = item.module || "—";
      tr.appendChild(module);

      var action = document.createElement("td");
      if (item.known) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "sf-btn small ghost";
        btn.textContent = "Reset";
        btn.addEventListener("click", function () { resetItem(item, btn); });
        action.appendChild(btn);
      }
      tr.appendChild(action);

      tbody.appendChild(tr);
    });
    $("sf-cbs-wrap").hidden = !(body.items || []).length;

    var chips = $("sf-cbs-modules");
    chips.textContent = "";
    (body.modules || []).forEach(function (m) {
      var chip = document.createElement("span");
      chip.className = "sf-chip";
      var name = document.createElement("b");
      name.textContent = (m.code || m.module) + " ";
      chip.appendChild(name);
      chip.appendChild(document.createTextNode(
        m.answered
          ? "CBS version " + (m.version === null || m.version === undefined ? "not reported" : m.version)
          : "no answer"
      ));
      chips.appendChild(chip);
    });
  }

  $("sf-cbs-read").addEventListener("click", function () {
    var btn = this;
    busy(btn, true, "Reading the car…");
    hide("sf-cbs-msg");
    api("/api/" + BRAND + "/cbs/read?mode=" + mode())
      .then(function (body) {
        renderCbs(body);
        say("sf-cbs-msg", label(body), tone(body));
      })
      .catch(function (err) { say("sf-cbs-msg", err.message, "bad"); })
      .then(function () { busy(btn, false); });
  });

  function resetItem(item, btn) {
    var extra = {};
    var dated = item.kennung === "TUV" || item.kennung === "AU";
    if (dated) {
      var when = window.prompt(
        "Target date for " + item.label + " — the month and year it is next due.\n" +
        "Format MM/YYYY, or leave blank to reset the counter only.", "");
      if (when === null) return;
      var match = when.trim().match(/^(\d{1,2})\s*[\/\-\.]\s*(\d{4})$/);
      if (when.trim() && !match) {
        say("sf-cbs-msg", "Couldn't read '" + when + "' as MM/YYYY — nothing was sent.", "bad");
        return;
      }
      if (match) {
        extra.target_month = parseInt(match[1], 10);
        extra.target_year = parseInt(match[2], 10);
      }
    }

    var warning = "Reset " + item.label + " on this car?\n\n" +
      "This writes to the car's " + (item.module || "service") + " module and cannot be undone. " +
      "It resets the service counter only — the iDrive service history is a separate record.";
    if (mode() === "sim") warning = "SIMULATED reset of " + item.label + " — no car will be touched.";
    if (!window.confirm(warning)) return;

    busy(btn, true, "Resetting…");
    var payload = { item: item.kennung, mode: mode(), confirm: true };
    Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
    api("/api/" + BRAND + "/cbs/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (body) {
        say("sf-cbs-msg", label(body), tone(body));
        if (body.ok && mode() === "real") $("sf-cbs-read").click();
      })
      .catch(function (err) { say("sf-cbs-msg", err.message, "bad"); })
      .then(function () { busy(btn, false); });
  }

  /* --------------------------------------------------------- battery --- */

  function renderBattery(body) {
    var grid = $("sf-batt-readings");
    grid.textContent = "";
    (body.readings || []).forEach(function (reading) {
      var card = document.createElement("div");
      card.className = "sf-reading";
      var key = document.createElement("span");
      key.className = "k";
      key.textContent = reading.name;
      card.appendChild(key);
      var value = document.createElement("span");
      value.className = "v";
      value.textContent = reading.value;
      card.appendChild(value);
      if (reading.unit) {
        var unit = document.createElement("span");
        unit.className = "u";
        unit.textContent = reading.unit;
        card.appendChild(unit);
      }
      grid.appendChild(card);
    });
  }

  $("sf-batt-read").addEventListener("click", function () {
    var btn = this;
    busy(btn, true, "Reading the car…");
    hide("sf-batt-msg");
    api("/api/" + BRAND + "/battery?mode=" + mode())
      .then(function (body) {
        renderBattery(body);
        say("sf-batt-msg", label(body), tone(body));
      })
      .catch(function (err) { say("sf-batt-msg", err.message, "bad"); })
      .then(function () { busy(btn, false); });
  });

  $("sf-batt-register").addEventListener("click", function () {
    var btn = this;
    var warning =
      "Register a battery replacement?\n\n" +
      "Only do this if a NEW battery has actually been fitted. It resets the " +
      "engine's charge history and cannot be undone.\n\n" +
      "It does not change the coded battery type or capacity.";
    if (mode() === "sim") warning = "SIMULATED registration — no car will be touched.";
    if (!window.confirm(warning)) return;

    busy(btn, true, "Registering…");
    api("/api/" + BRAND + "/battery/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode(), confirm: true })
    })
      .then(function (body) {
        say("sf-batt-msg", label(body), tone(body));
        if (body.ok && mode() === "real") $("sf-batt-read").click();
      })
      .catch(function (err) { say("sf-batt-msg", err.message, "bad"); })
      .then(function () { busy(btn, false); });
  });

  /* --------------------------------------------------------- gateway --- */

  $("sf-ecu-read").addEventListener("click", function () {
    var btn = this;
    busy(btn, true, "Asking the gateway…");
    hide("sf-ecu-msg");
    api("/api/" + BRAND + "/ediabas/installed?mode=" + mode())
      .then(function (body) {
        var tbody = $("sf-ecu-body");
        tbody.textContent = "";
        (body.modules || []).forEach(function (m) {
          var tr = document.createElement("tr");
          var cells = [
            { text: m.name || "—", cls: "name" },
            { text: m.address || "—", cls: "mono" },
            { text: m.known ? (m.code + " · " + m.label) : "not mapped in the harness yet", cls: "" },
            { text: (m.sgbds || []).join(", ") || "—", cls: "mono" }
          ];
          cells.forEach(function (c) {
            var td = document.createElement("td");
            td.className = c.cls;
            td.textContent = c.text;
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        $("sf-ecu-wrap").hidden = !(body.modules || []).length;
        say("sf-ecu-msg", label(body), tone(body));
      })
      .catch(function (err) { say("sf-ecu-msg", err.message, "bad"); })
      .then(function () { busy(btn, false); });
  });
})();
