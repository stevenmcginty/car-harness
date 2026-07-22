(function () {
  "use strict";

  // ---------- Constants ----------

  var STEP_ORDER = ["adapter", "link", "doip", "vin"];
  var DEFAULT_STEPS = [
    { key: "adapter", label: "Ethernet adapter", state: "pending", detail: "Waiting for backend…" },
    { key: "link", label: "Link to car", state: "pending", detail: "Waiting for backend…" },
    { key: "doip", label: "Car responding", state: "pending", detail: "Waiting for backend…" },
    { key: "vin", label: "Vehicle identified", state: "pending", detail: "Waiting for backend…" }
  ];
  var AUTO_REFRESH_MS = 4000;
  var DEFAULT_PROFILE_ID = "bmw";

  var ICON_OK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 12 9 17 20 6"></polyline></svg>';
  var ICON_FAIL =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line></svg>';
  var ICON_PENDING = '<span class="spinner"></span>';

  // ---------- State ----------

  var state = {
    profiles: [],
    currentProfileId: DEFAULT_PROFILE_ID,
    simulate: false,
    autoRefresh: false,
    autoRefreshTimer: null,
    backendReachable: null, // null = unknown yet, true/false once we know
    testInFlight: false,
    adapters: [],
    currentAdapterId: "" // "" = Auto / none
  };

  // Add-adapter wizard state. step 1 = choose type + manage existing,
  // 2 = configure, 3 = test, 4 = name & save.
  var wizard = {
    open: false,
    step: 1,
    types: [],
    typesLoaded: false, // false until a fetch has resolved (ok or empty)
    ports: [],
    selectedTypeId: null,
    config: {},
    name: "",
    testResult: null, // { ok, detail } | null
    testInFlight: false,
    saveInFlight: false,
    portsRequested: false,
    error: ""
  };

  // ---------- DOM refs ----------

  var el = {
    profileSelect: document.getElementById("profile-select"),
    simulateToggle: document.getElementById("simulate-toggle"),
    autoRefreshToggle: document.getElementById("autorefresh-toggle"),
    testBtn: document.getElementById("test-btn"),
    backendBanner: document.getElementById("backend-banner"),
    simulatedBanner: document.getElementById("simulated-banner"),
    overallBadge: document.getElementById("overall-badge"),
    overallLabel: document.getElementById("overall-label"),
    statusMessage: document.getElementById("status-message"),
    ladder: document.getElementById("ladder"),
    vehicleCard: document.getElementById("vehicle-card"),
    vehicleVin: document.getElementById("vehicle-vin"),
    vehicleAddress: document.getElementById("vehicle-address"),
    vehicleEid: document.getElementById("vehicle-eid"),
    vehicleGid: document.getElementById("vehicle-gid"),
    importBtn: document.getElementById("import-btn"),
    importFileInput: document.getElementById("import-file-input"),
    modulesEmpty: document.getElementById("modules-empty"),
    modulesList: document.getElementById("modules-list"),
    footerStatus: document.getElementById("footer-status"),
    adapterSelect: document.getElementById("adapter-select"),
    addAdapterBtn: document.getElementById("add-adapter-btn"),
    modal: document.getElementById("adapter-modal"),
    modalClose: document.getElementById("adapter-modal-close"),
    modalBody: document.getElementById("adapter-modal-body"),
    wizardProgress: document.getElementById("wizard-progress"),
    wizardBack: document.getElementById("wizard-back"),
    wizardCancel: document.getElementById("wizard-cancel"),
    wizardNext: document.getElementById("wizard-next"),
    obdScanBtn: document.getElementById("obd-scan-btn"),
    obdClearBtn: document.getElementById("obd-clear-btn"),
    obdSimBanner: document.getElementById("obd-sim-banner"),
    obdHint: document.getElementById("obd-hint"),
    obdResult: document.getElementById("obd-result"),
    obdChips: document.getElementById("obd-chips"),
    obdMessage: document.getElementById("obd-message"),
    obdDtcList: document.getElementById("obd-dtc-list"),
    liveToggle: document.getElementById("live-toggle"),
    liveMessage: document.getElementById("live-message"),
    liveGrid: document.getElementById("live-grid"),
    bmwScanBtn: document.getElementById("bmw-scan-btn"),
    bmwSimBanner: document.getElementById("bmw-sim-banner"),
    bmwResult: document.getElementById("bmw-result"),
    bmwChips: document.getElementById("bmw-chips"),
    bmwMessage: document.getElementById("bmw-message"),
    bmwModules: document.getElementById("bmw-modules"),
    mazdaScanBtn: document.getElementById("mazda-scan-btn"),
    mazdaLiveBtn: document.getElementById("mazda-live-btn"),
    mazdaSnapBtn: document.getElementById("mazda-snap-btn"),
    mazdaSimBanner: document.getElementById("mazda-sim-banner"),
    mazdaResult: document.getElementById("mazda-result"),
    mazdaChips: document.getElementById("mazda-chips"),
    mazdaMessage: document.getElementById("mazda-message"),
    mazdaModules: document.getElementById("mazda-modules"),
    mazdaExtras: document.getElementById("mazda-extras"),
    mazdaExtrasMessage: document.getElementById("mazda-extras-message"),
    mazdaExtrasChips: document.getElementById("mazda-extras-chips"),
    serviceBrandBmw: document.getElementById("service-brand-bmw"),
    serviceBrandMazda: document.getElementById("service-brand-mazda"),
    serviceBrandPeugeot: document.getElementById("service-brand-peugeot"),
    serviceMessage: document.getElementById("service-message"),
    serviceList: document.getElementById("service-list"),
    pairBtn: document.getElementById("pair-btn"),
    pairModal: document.getElementById("pair-modal"),
    pairModalClose: document.getElementById("pair-modal-close"),
    pairModalBody: document.getElementById("pair-modal-body")
  };

  // OBD-II + pairing state.
  var obd = {
    scanInFlight: false,
    clearInFlight: false,
    hasScan: false,
    liveOn: false,
    liveTimer: null,
    liveInFlight: false
  };
  var pair = { open: false, info: null, devices: [], error: "" };
  var bmw = { scanInFlight: false, clearInFlight: false };
  var mazda = { scanInFlight: false, clearInFlight: false, liveInFlight: false };
  var service = { brand: "", loading: false, runningId: null };

  // ---------- Helpers ----------

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return "—";
    if (bytes < 1024) return bytes + " B";
    var kb = bytes / 1024;
    if (kb < 1024) return kb.toFixed(kb < 10 ? 1 : 0) + " KB";
    var mb = kb / 1024;
    return mb.toFixed(mb < 10 ? 1 : 0) + " MB";
  }

  function titleCase(str) {
    if (!str) return "";
    return String(str)
      .split(/[_\s-]+/)
      .map(function (w) {
        return w.charAt(0).toUpperCase() + w.slice(1);
      })
      .join(" ");
  }

  // Wraps fetch: resolves with parsed JSON (or null for empty body),
  // rejects on network failure or non-2xx response. Tracks backend reachability.
  // Two-callback .then so only fetch()'s own rejection (network down) marks the
  // backend unreachable -- an HTTP 4xx/5xx means the backend IS running.
  function apiFetch(url, options) {
    return fetch(url, options).then(
      function (res) {
        setBackendReachable(true);
        if (!res.ok) {
          throw new Error("HTTP " + res.status);
        }
        return res
          .text()
          .then(function (text) {
            if (!text) return null;
            try {
              return JSON.parse(text);
            } catch (e) {
              return null;
            }
          });
      },
      function (networkErr) {
        setBackendReachable(false);
        throw networkErr;
      }
    );
  }

  var reconnectTimer = null;

  function setBackendReachable(reachable) {
    if (state.backendReachable === reachable) return;
    state.backendReachable = reachable;
    el.backendBanner.hidden = reachable;
    if (reachable) {
      if (reconnectTimer) {
        clearInterval(reconnectTimer);
        reconnectTimer = null;
      }
    } else {
      // Backend lost: everything on screen is stale. Reset the panels so the
      // banner never contradicts old results, then poll until it comes back.
      renderStatusOffline();
      resetObdPanel();
      resetBmwPanel();
      resetMazdaPanel();
      startReconnectPoll();
    }
  }

  function startReconnectPoll() {
    if (reconnectTimer) return;
    reconnectTimer = setInterval(function () {
      fetch("/api/ping")
        .then(function (res) {
          if (!res.ok) return;
          setBackendReachable(true);
          refreshAll();
          loadAdapters();
        })
        .catch(function () {
          /* still down; keep polling */
        });
    }, 3000);
  }

  // ---------- Rendering: header / profiles ----------

  function renderProfileOptions() {
    var select = el.profileSelect;
    select.innerHTML = "";

    if (!state.profiles.length) {
      var opt = document.createElement("option");
      opt.value = DEFAULT_PROFILE_ID;
      opt.textContent = "BMW";
      select.appendChild(opt);
      select.disabled = true;
      return;
    }

    state.profiles.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name;
      if (p.id === state.currentProfileId) o.selected = true;
      select.appendChild(o);
    });
    select.disabled = false;
  }

  function currentProfile() {
    for (var i = 0; i < state.profiles.length; i++) {
      if (state.profiles[i].id === state.currentProfileId) return state.profiles[i];
    }
    return null;
  }

  // ---------- Rendering: connection ladder / badge ----------

  function renderLadder(steps) {
    var ordered = steps.slice().sort(function (a, b) {
      var ia = STEP_ORDER.indexOf(a.key);
      var ib = STEP_ORDER.indexOf(b.key);
      if (ia === -1) ia = STEP_ORDER.length;
      if (ib === -1) ib = STEP_ORDER.length;
      return ia - ib;
    });

    el.ladder.innerHTML = "";
    ordered.forEach(function (step) {
      var li = document.createElement("li");
      li.className = "ladder-step";

      var iconWrap = document.createElement("span");
      var iconHtml, iconClass;
      if (step.state === "ok") {
        iconHtml = ICON_OK;
        iconClass = "icon-ok";
      } else if (step.state === "fail") {
        iconHtml = ICON_FAIL;
        iconClass = "icon-fail";
      } else {
        iconHtml = ICON_PENDING;
        iconClass = "icon-pending";
      }
      iconWrap.className = "step-icon " + iconClass;
      iconWrap.innerHTML = iconHtml;

      var body = document.createElement("div");
      body.className = "step-body";

      var label = document.createElement("span");
      label.className = "step-label";
      label.textContent = step.label || step.key;

      var detail = document.createElement("span");
      detail.className = "step-detail";
      detail.textContent = step.detail || "";

      body.appendChild(label);
      body.appendChild(detail);

      li.appendChild(iconWrap);
      li.appendChild(body);
      el.ladder.appendChild(li);
    });
  }

  function renderOverall(state_, message) {
    var validStates = { connected: 1, partial: 1, disconnected: 1 };
    var cls = validStates[state_] ? state_ : "offline";
    var label;
    if (cls === "connected") label = "CONNECTED";
    else if (cls === "partial") label = "PARTIAL";
    else if (cls === "disconnected") label = "DISCONNECTED";
    else label = "NO BACKEND";

    el.overallBadge.className = "overall-badge state-" + cls;
    el.overallLabel.textContent = label;
    el.statusMessage.textContent = message || "";
  }

  function renderVehicle(vehicle) {
    if (!vehicle) {
      el.vehicleCard.hidden = true;
      return;
    }
    el.vehicleCard.hidden = false;
    el.vehicleVin.textContent = vehicle.vin || "—";
    el.vehicleAddress.textContent = vehicle.logicalAddress || "—";
    el.vehicleEid.textContent = vehicle.eid || "—";
    el.vehicleGid.textContent = vehicle.gid || "—";
  }

  function renderSimulatedBanner(simulated) {
    el.simulatedBanner.hidden = !simulated;
  }

  function renderStatusOffline() {
    renderLadder(DEFAULT_STEPS);
    renderOverall("offline", "");
    renderVehicle(null);
    renderSimulatedBanner(false);
  }

  // ---------- Rendering: modules ----------

  function renderModules(modules) {
    el.modulesList.innerHTML = "";

    if (!modules.length) {
      el.modulesEmpty.hidden = false;
      return;
    }
    el.modulesEmpty.hidden = true;

    modules.forEach(function (m) {
      var card = document.createElement("div");
      card.className = "module-card";

      var header = document.createElement("div");
      header.className = "module-card-header";

      var name = document.createElement("span");
      name.className = "module-name";
      name.textContent = m.name || m.id;
      header.appendChild(name);

      if (m.builtin) {
        var tag = document.createElement("span");
        tag.className = "tag tag-builtin";
        tag.textContent = "built-in";
        header.appendChild(tag);
      }
      card.appendChild(header);

      if (m.description) {
        var desc = document.createElement("p");
        desc.className = "module-description";
        desc.textContent = m.description;
        card.appendChild(desc);
      }

      var meta = document.createElement("div");
      meta.className = "module-meta";

      var chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = m.status ? titleCase(m.status) : (m.installed ? "Installed" : "Not installed");
      meta.appendChild(chip);

      var size = document.createElement("span");
      size.className = "module-size";
      size.textContent = formatBytes(m.sizeBytes);
      meta.appendChild(size);

      card.appendChild(meta);

      if (!m.builtin) {
        var actions = document.createElement("div");
        actions.className = "module-actions";

        var delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "btn btn-danger btn-small";
        delBtn.textContent = "Delete";
        delBtn.addEventListener("click", function () {
          deleteModule(m.id);
        });

        actions.appendChild(delBtn);
        card.appendChild(actions);
      }

      el.modulesList.appendChild(card);
    });
  }

  // ---------- Data loading ----------

  function loadProfiles() {
    return apiFetch("/api/profiles")
      .then(function (data) {
        if (!data || !data.profiles) return;
        state.profiles = data.profiles;
        if (!currentProfile() && state.profiles.length) {
          state.currentProfileId = state.profiles[0].id;
        }
        renderProfileOptions();
      })
      .catch(function () {
        renderProfileOptions();
      });
  }

  function refreshStatus() {
    var mode = state.simulate ? "sim" : "real";
    var url = "/api/status?profile=" + encodeURIComponent(state.currentProfileId) + "&mode=" + mode;
    if (state.currentAdapterId) {
      url += "&adapter=" + encodeURIComponent(state.currentAdapterId);
    }
    return apiFetch(url)
      .then(function (data) {
        if (!data) {
          renderStatusOffline();
          return;
        }
        renderLadder(data.steps || []);
        renderOverall(data.overall, data.message);
        renderVehicle(data.vehicle);
        renderSimulatedBanner(!!data.simulated);
      })
      .catch(function () {
        renderStatusOffline();
      });
  }

  function loadModules() {
    return apiFetch("/api/modules")
      .then(function (data) {
        var all = (data && data.modules) || [];
        var profile = currentProfile();
        var descMap = {};
        if (profile && profile.modules) {
          profile.modules.forEach(function (pm) {
            descMap[pm.id] = pm;
          });
        }

        var filtered = all
          .filter(function (m) {
            return m.profile === state.currentProfileId;
          })
          .map(function (m) {
            var extra = descMap[m.id] || {};
            return {
              id: m.id,
              name: m.name,
              builtin: m.builtin,
              installed: m.installed,
              sizeBytes: m.sizeBytes,
              description: extra.description,
              status: extra.status
            };
          });

        renderModules(filtered);
      })
      .catch(function () {
        el.modulesList.innerHTML = "";
        el.modulesEmpty.hidden = false;
      });
  }

  function refreshAll() {
    return Promise.all([refreshStatus(), loadModules()]);
  }

  function deleteModule(id) {
    if (!window.confirm("Delete this job? This cannot be undone.")) return;
    apiFetch("/api/modules/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function () {
        loadModules();
      })
      .catch(function () {
        /* backend banner already surfaced by apiFetch */
      });
  }

  function importModule(file) {
    var formData = new FormData();
    formData.append("file", file);
    el.importBtn.disabled = true;
    var originalText = el.importBtn.textContent;
    el.importBtn.textContent = "Importing…";

    apiFetch("/api/modules/import", { method: "POST", body: formData })
      .then(function () {
        loadModules();
      })
      .catch(function () {
        /* backend banner already surfaced by apiFetch */
      })
      .then(function () {
        el.importBtn.disabled = false;
        el.importBtn.textContent = originalText;
      });
  }

  // ---------- Adapters: header select ----------

  function renderAdapterSelect() {
    var select = el.adapterSelect;
    var previous = state.currentAdapterId;
    select.innerHTML = "";

    var none = document.createElement("option");
    none.value = "";
    none.textContent = "Auto / none";
    select.appendChild(none);

    var stillExists = false;
    state.adapters.forEach(function (a) {
      var o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.name || a.id;
      select.appendChild(o);
      if (a.id === previous) stillExists = true;
    });

    if (!stillExists) state.currentAdapterId = "";
    select.value = state.currentAdapterId;
  }

  function loadAdapters() {
    return apiFetch("/api/adapters")
      .then(function (data) {
        state.adapters = (data && data.adapters) || [];
        renderAdapterSelect();
        if (wizard.open) renderWizard();
      })
      .catch(function () {
        // Backend down: keep an empty list; "Auto / none" stays usable.
        state.adapters = [];
        renderAdapterSelect();
        if (wizard.open) renderWizard();
      });
  }

  function deleteAdapter(id) {
    if (!window.confirm("Delete this adapter? This cannot be undone.")) return;
    apiFetch("/api/adapters/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function () {
        loadAdapters();
      })
      .catch(function () {
        /* backend banner already surfaced by apiFetch */
      });
  }

  // ---------- Add-adapter wizard ----------

  function selectedType() {
    for (var i = 0; i < wizard.types.length; i++) {
      if (wizard.types[i].id === wizard.selectedTypeId) return wizard.types[i];
    }
    return null;
  }

  function adapterTypeName(typeId) {
    for (var i = 0; i < wizard.types.length; i++) {
      if (wizard.types[i].id === typeId) return wizard.types[i].name;
    }
    return typeId || "";
  }

  function openWizard() {
    wizard.open = true;
    wizard.step = 1;
    wizard.selectedTypeId = null;
    wizard.config = {};
    wizard.name = "";
    wizard.testResult = null;
    wizard.testInFlight = false;
    wizard.saveInFlight = false;
    wizard.portsRequested = false;
    wizard.error = "";
    el.modal.hidden = false;
    renderWizard();
    loadAdapterTypes();
    loadAdapters();
  }

  function closeWizard() {
    wizard.open = false;
    el.modal.hidden = true;
  }

  function loadAdapterTypes() {
    return apiFetch("/api/adapter-types")
      .then(function (data) {
        wizard.types = (data && data.types) || [];
        wizard.typesLoaded = true;
        if (wizard.open) renderWizard();
      })
      .catch(function () {
        // Leave wizard.types as-is; typesLoaded stays false if we never succeeded.
        if (wizard.open) renderWizard();
      });
  }

  function loadSerialPorts() {
    return apiFetch("/api/serial-ports")
      .then(function (data) {
        wizard.ports = (data && data.ports) || [];
        if (wizard.open) renderWizard();
      })
      .catch(function () {
        wizard.ports = [];
        if (wizard.open) renderWizard();
      });
  }

  function portListHas(device) {
    for (var i = 0; i < wizard.ports.length; i++) {
      if (wizard.ports[i].device === device) return true;
    }
    return false;
  }

  // --- Wizard rendering ---

  function renderWizard() {
    renderWizardProgress();
    el.modalBody.innerHTML = "";
    if (wizard.step === 1) renderStep1();
    else if (wizard.step === 2) renderStep2();
    else if (wizard.step === 3) renderStep3();
    else if (wizard.step === 4) renderStep4();
    renderWizardFooter();
  }

  function renderWizardProgress() {
    var labels = ["Type", "Configure", "Test", "Save"];
    el.wizardProgress.innerHTML = "";
    labels.forEach(function (lbl, i) {
      var n = i + 1;
      var li = document.createElement("li");
      if (n === wizard.step) li.className = "is-active";
      else if (n < wizard.step) li.className = "is-done";

      var num = document.createElement("span");
      num.className = "wizard-step-num";
      num.textContent = String(n);

      var text = document.createElement("span");
      text.textContent = lbl;

      li.appendChild(num);
      li.appendChild(text);
      el.wizardProgress.appendChild(li);
    });
  }

  function renderStep1() {
    // Manage existing adapters
    var manage = document.createElement("div");
    manage.className = "wizard-section";

    var mTitle = document.createElement("p");
    mTitle.className = "wizard-section-title";
    mTitle.textContent = "Configured adapters";
    manage.appendChild(mTitle);

    if (state.adapters.length) {
      var list = document.createElement("div");
      list.className = "adapter-list";
      state.adapters.forEach(function (a) {
        list.appendChild(buildAdapterListItem(a));
      });
      manage.appendChild(list);
    } else {
      var noneMsg = document.createElement("p");
      noneMsg.className = "wizard-empty";
      noneMsg.textContent = "No adapters configured yet.";
      manage.appendChild(noneMsg);
    }
    el.modalBody.appendChild(manage);

    // Choose a type to add
    var choose = document.createElement("div");
    choose.className = "wizard-section";

    var cTitle = document.createElement("p");
    cTitle.className = "wizard-section-title";
    cTitle.textContent = "Add a new adapter — choose a type";
    choose.appendChild(cTitle);

    if (wizard.types.length) {
      var choices = document.createElement("div");
      choices.className = "type-choices";
      wizard.types.forEach(function (t) {
        choices.appendChild(buildTypeChoice(t));
      });
      choose.appendChild(choices);
    } else {
      var empty = document.createElement("p");
      empty.className = "wizard-empty";
      if (!wizard.typesLoaded && state.backendReachable !== false) {
        empty.textContent = "Loading adapter types…";
      } else {
        empty.textContent = "Couldn't load adapter types — is the backend running?";
      }
      choose.appendChild(empty);
    }
    el.modalBody.appendChild(choose);
  }

  function buildAdapterListItem(a) {
    var item = document.createElement("div");
    item.className = "adapter-list-item";

    var info = document.createElement("div");
    info.className = "adapter-list-info";

    var name = document.createElement("span");
    name.className = "adapter-list-name";
    name.textContent = a.name || a.id;
    info.appendChild(name);

    var meta = document.createElement("span");
    meta.className = "adapter-list-meta";
    meta.textContent = adapterTypeName(a.typeId);
    info.appendChild(meta);

    if (a.lastTest) {
      var test = document.createElement("span");
      test.className = "adapter-list-test " + (a.lastTest.ok ? "is-ok" : "is-fail");
      var label = a.lastTest.ok ? "Last test OK" : "Last test failed";
      if (a.lastTest.detail) label += " — " + a.lastTest.detail;
      test.textContent = label;
      info.appendChild(test);
    }

    item.appendChild(info);

    var del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-danger btn-small";
    del.textContent = "Delete";
    del.addEventListener("click", function () {
      deleteAdapter(a.id);
    });
    item.appendChild(del);

    return item;
  }

  function buildTypeChoice(t) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "type-choice" + (t.id === wizard.selectedTypeId ? " is-selected" : "");

    var name = document.createElement("span");
    name.className = "type-choice-name";
    name.textContent = t.name || t.id;
    btn.appendChild(name);

    if (t.description) {
      var desc = document.createElement("span");
      desc.className = "type-choice-desc";
      desc.textContent = t.description;
      btn.appendChild(desc);
    }

    btn.addEventListener("click", function () {
      selectType(t.id);
    });
    return btn;
  }

  function selectType(id) {
    if (wizard.selectedTypeId !== id) {
      wizard.selectedTypeId = id;
      wizard.config = {}; // reset when the type changes
      wizard.testResult = null;
      wizard.error = "";
    }
    renderWizard();
  }

  function renderStep2() {
    var type = selectedType();
    if (!type) {
      var guard = document.createElement("p");
      guard.className = "wizard-empty";
      guard.textContent = "Select an adapter type first.";
      el.modalBody.appendChild(guard);
      return;
    }

    var hint = document.createElement("p");
    hint.className = "wizard-hint";
    hint.textContent = "Configure " + (type.name || type.id) + ".";
    el.modalBody.appendChild(hint);

    var form = document.createElement("div");
    form.className = "wizard-form";
    (type.fields || []).forEach(function (field) {
      form.appendChild(buildFieldRow(field));
    });
    el.modalBody.appendChild(form);

    if (wizard.error) {
      var err = document.createElement("p");
      err.className = "wizard-error";
      err.textContent = wizard.error;
      el.modalBody.appendChild(err);
    }
  }

  function buildFieldRow(field) {
    var fieldId = "wf-" + field.key;

    var row = document.createElement("div");
    row.className = "form-row";

    var label = document.createElement("label");
    label.className = "form-row-label";
    label.htmlFor = fieldId;
    label.textContent = field.label || field.key;
    if (field.required) {
      var req = document.createElement("span");
      req.className = "req";
      req.textContent = "*";
      label.appendChild(req);
    }
    row.appendChild(label);

    var value = wizard.config[field.key];
    if (value === undefined || value === null) value = "";

    if (field.type === "select") {
      var sel = document.createElement("select");
      sel.className = "form-input";
      sel.id = fieldId;
      var ph = document.createElement("option");
      ph.value = "";
      ph.textContent = "Select…";
      sel.appendChild(ph);
      (field.options || []).forEach(function (opt) {
        var o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        sel.appendChild(o);
      });
      sel.value = value;
      sel.addEventListener("change", function () {
        wizard.config[field.key] = sel.value;
      });
      row.appendChild(sel);
    } else if (field.type === "comport") {
      row.appendChild(buildComportControl(field, value, fieldId));
    } else {
      var input = document.createElement("input");
      input.type = "text";
      input.className = "form-input";
      input.id = fieldId;
      input.value = value;
      if (field.type === "file") {
        input.placeholder = "paste the full path";
      } else if (field.type === "ip") {
        input.placeholder = "e.g. 169.254.10.5";
      }
      input.addEventListener("input", function () {
        wizard.config[field.key] = input.value;
      });
      row.appendChild(input);
    }

    var helpText = field.help;
    if (field.type === "file" && !helpText) helpText = "Paste the full path to the file.";
    if (helpText) {
      var help = document.createElement("span");
      help.className = "form-row-help";
      help.textContent = helpText;
      row.appendChild(help);
    }

    return row;
  }

  function buildComportControl(field, value, fieldId) {
    var wrap = document.createElement("div");
    wrap.className = "form-comport-row";

    var sel = document.createElement("select");
    sel.className = "form-input";
    sel.id = fieldId;

    var ph = document.createElement("option");
    ph.value = "";
    ph.textContent = wizard.ports.length ? "Select a port…" : "No ports found";
    sel.appendChild(ph);

    wizard.ports.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p.device;
      o.textContent = p.device + (p.description ? " — " + p.description : "");
      sel.appendChild(o);
    });

    // Preserve a previously chosen value even if it's not in the current port list.
    if (value && !portListHas(value)) {
      var kept = document.createElement("option");
      kept.value = value;
      kept.textContent = value;
      sel.appendChild(kept);
    }

    sel.value = value;
    sel.addEventListener("change", function () {
      wizard.config[field.key] = sel.value;
    });
    wrap.appendChild(sel);

    var refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "btn btn-secondary btn-small";
    refresh.textContent = "Refresh ports";
    refresh.addEventListener("click", function () {
      loadSerialPorts();
    });
    wrap.appendChild(refresh);

    return wrap;
  }

  function renderStep3() {
    var type = selectedType();

    var hint = document.createElement("p");
    hint.className = "wizard-hint";
    hint.textContent = "Test the connection before saving. You can save even if the test fails.";
    el.modalBody.appendChild(hint);

    var testBtn = document.createElement("button");
    testBtn.type = "button";
    testBtn.className = "btn btn-primary";
    testBtn.textContent = wizard.testInFlight ? "Testing…" : "Test";
    testBtn.disabled = wizard.testInFlight || !type;
    testBtn.addEventListener("click", runAdapterTest);
    el.modalBody.appendChild(testBtn);

    if (wizard.testResult) {
      el.modalBody.appendChild(buildTestResult(wizard.testResult));
    }
    if (wizard.error) {
      var err = document.createElement("p");
      err.className = "wizard-error";
      err.textContent = wizard.error;
      el.modalBody.appendChild(err);
    }
  }

  function buildTestResult(result) {
    var box = document.createElement("div");
    box.className = "test-result " + (result.ok ? "is-ok" : "is-fail");

    var icon = document.createElement("span");
    icon.className = "test-result-icon";
    icon.innerHTML = result.ok ? ICON_OK : ICON_FAIL; // static constant markup only

    var detail = document.createElement("span");
    detail.className = "test-result-detail";
    detail.textContent = result.detail || (result.ok ? "Connection OK." : "Test failed.");

    box.appendChild(icon);
    box.appendChild(detail);
    return box;
  }

  function runAdapterTest() {
    if (wizard.testInFlight) return;
    var type = selectedType();
    if (!type) return;

    wizard.testInFlight = true;
    wizard.error = "";
    renderWizard();

    apiFetch("/api/adapters/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ typeId: type.id, config: wizard.config })
    })
      .then(function (data) {
        wizard.testInFlight = false;
        if (data && typeof data.ok === "boolean") {
          wizard.testResult = { ok: data.ok, detail: data.detail || "" };
        } else {
          wizard.testResult = { ok: false, detail: "Unexpected response from backend." };
        }
        renderWizard();
      })
      .catch(function () {
        wizard.testInFlight = false;
        wizard.testResult = { ok: false, detail: "Couldn't reach the backend to run the test." };
        renderWizard();
      });
  }

  function renderStep4() {
    var hint = document.createElement("p");
    hint.className = "wizard-hint";
    hint.textContent = "Give this adapter a name and save it.";
    el.modalBody.appendChild(hint);

    var row = document.createElement("div");
    row.className = "form-row";

    var label = document.createElement("label");
    label.className = "form-row-label";
    label.htmlFor = "wizard-name-input";
    label.textContent = "Name";
    var req = document.createElement("span");
    req.className = "req";
    req.textContent = "*";
    label.appendChild(req);
    row.appendChild(label);

    var input = document.createElement("input");
    input.type = "text";
    input.id = "wizard-name-input";
    input.className = "form-input";
    input.value = wizard.name || "";
    input.addEventListener("input", function () {
      wizard.name = input.value;
    });
    row.appendChild(input);
    el.modalBody.appendChild(row);

    if (wizard.error) {
      var err = document.createElement("p");
      err.className = "wizard-error";
      err.textContent = wizard.error;
      el.modalBody.appendChild(err);
    }
  }

  function renderWizardFooter() {
    var step = wizard.step;
    el.wizardBack.hidden = step === 1;
    el.wizardNext.textContent = step === 4 ? (wizard.saveInFlight ? "Saving…" : "Save") : "Next";

    var disabled = false;
    if (step === 1) disabled = !wizard.selectedTypeId;
    else if (step === 4) disabled = wizard.saveInFlight;
    el.wizardNext.disabled = disabled;
  }

  function goToStep(n) {
    wizard.step = n;
    wizard.error = "";
    if (n === 2) {
      var type = selectedType();
      var hasComport = !!(type && (type.fields || []).some(function (f) {
        return f.type === "comport";
      }));
      if (hasComport && !wizard.portsRequested) {
        wizard.portsRequested = true;
        loadSerialPorts();
      }
    }
    if (n === 4) {
      var t = selectedType();
      if (!wizard.name && t) wizard.name = t.name || "";
    }
    renderWizard();
  }

  function validateStep2() {
    var type = selectedType();
    if (!type) return false;
    var missing = [];
    (type.fields || []).forEach(function (f) {
      if (f.required) {
        var v = wizard.config[f.key];
        if (v === undefined || v === null || String(v).trim() === "") {
          missing.push(f.label || f.key);
        }
      }
    });
    if (missing.length) {
      wizard.error = "Please fill in: " + missing.join(", ");
      return false;
    }
    wizard.error = "";
    return true;
  }

  function saveAdapter() {
    if (wizard.saveInFlight) return;
    var type = selectedType();
    if (!type) return;

    var name = (wizard.name || "").trim();
    if (!name) {
      wizard.error = "Please enter a name.";
      renderWizard();
      return;
    }

    wizard.saveInFlight = true;
    wizard.error = "";
    renderWizard();

    apiFetch("/api/adapters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ typeId: type.id, name: name, config: wizard.config })
    })
      .then(function (saved) {
        wizard.saveInFlight = false;
        closeWizard();
        return loadAdapters().then(function () {
          if (saved && saved.id) {
            state.currentAdapterId = saved.id;
            renderAdapterSelect();
            refreshStatus();
          }
        });
      })
      .catch(function () {
        wizard.saveInFlight = false;
        wizard.error = "Couldn't save — is the backend running?";
        renderWizard();
      });
  }

  function wizardAdvance() {
    if (wizard.step === 1) {
      if (!wizard.selectedTypeId) return;
      goToStep(2);
    } else if (wizard.step === 2) {
      if (!validateStep2()) {
        renderWizard();
        return;
      }
      goToStep(3);
    } else if (wizard.step === 3) {
      goToStep(4);
    } else if (wizard.step === 4) {
      saveAdapter();
    }
  }

  function wizardGoBack() {
    if (wizard.step > 1) {
      wizard.error = "";
      goToStep(wizard.step - 1);
    }
  }

  // ---------- OBD-II (all cars) ----------

  function obdMode() {
    return state.simulate ? "sim" : "real";
  }

  // Clears scan results and the SIMULATED banner. Called whenever the results
  // on screen can no longer match reality: backend lost, Simulate toggled, or
  // a different adapter picked.
  function resetObdPanel() {
    obd.hasScan = false;
    el.obdClearBtn.disabled = true;
    el.obdResult.hidden = true;
    el.obdSimBanner.hidden = true;
    el.obdChips.innerHTML = "";
    el.obdDtcList.innerHTML = "";
    el.obdMessage.textContent = "";
  }

  function obdUrl(path) {
    var url = path + "?mode=" + obdMode();
    if (!state.simulate && state.currentAdapterId) {
      url += "&adapter=" + encodeURIComponent(state.currentAdapterId);
    }
    return url;
  }

  function chip(label, value, cls) {
    var span = document.createElement("span");
    span.className = "obd-chip" + (cls ? " " + cls : "");
    var l = document.createElement("span");
    l.className = "obd-chip-label";
    l.textContent = label;
    var v = document.createElement("span");
    v.className = "obd-chip-value";
    v.textContent = value;
    span.appendChild(l);
    span.appendChild(v);
    return span;
  }

  function renderObdScan(data) {
    el.obdResult.hidden = false;
    el.obdSimBanner.hidden = !(data && data.simulated);
    el.obdChips.innerHTML = "";
    el.obdDtcList.innerHTML = "";
    el.obdMessage.textContent = (data && data.message) || "";

    if (!data || !data.ok) {
      obd.hasScan = false;
      el.obdClearBtn.disabled = true;
      return;
    }
    obd.hasScan = true;
    el.obdClearBtn.disabled = false;

    if (data.vin) el.obdChips.appendChild(chip("VIN", data.vin, "is-mono"));
    if (data.protocol) el.obdChips.appendChild(chip("Protocol", data.protocol));
    if (data.voltage) el.obdChips.appendChild(chip("Battery", data.voltage));
    if (data.firmware) el.obdChips.appendChild(chip("Dongle", data.firmware));
    el.obdChips.appendChild(
      chip("Engine light", data.milOn ? "ON" : "off", data.milOn ? "is-bad" : "is-good")
    );

    var groups = [
      { list: data.stored || [], cls: "is-stored" },
      { list: data.pending || [], cls: "is-pending" },
      { list: data.permanent || [], cls: "is-permanent" }
    ];
    var any = false;
    groups.forEach(function (g) {
      g.list.forEach(function (d) {
        any = true;
        var card = document.createElement("div");
        card.className = "dtc-card";

        var head = document.createElement("div");
        head.className = "dtc-card-head";

        var code = document.createElement("span");
        code.className = "dtc-code";
        code.textContent = d.code;
        head.appendChild(code);

        var tag = document.createElement("span");
        tag.className = "tag dtc-tag " + g.cls;
        tag.textContent = d.status;
        head.appendChild(tag);

        card.appendChild(head);

        var desc = document.createElement("p");
        desc.className = "dtc-desc";
        desc.textContent = d.description || "";
        card.appendChild(desc);

        el.obdDtcList.appendChild(card);
      });
    });
    if (!any) {
      var none = document.createElement("p");
      none.className = "dtc-none";
      none.textContent = "✓ No fault codes.";
      el.obdDtcList.appendChild(none);
    }
  }

  function runObdScan() {
    if (obd.scanInFlight) return;
    if (!state.simulate && !state.currentAdapterId) {
      el.obdResult.hidden = false;
      el.obdMessage.textContent =
        "Pick an ELM327-style adapter (top right) first, or turn on Simulate.";
      return;
    }
    obd.scanInFlight = true;
    el.obdScanBtn.disabled = true;
    var original = el.obdScanBtn.textContent;
    el.obdScanBtn.textContent = "Scanning…";
    el.obdResult.hidden = false;
    el.obdMessage.textContent = "Talking to the car… (first contact can take ~20s over Bluetooth)";
    el.obdChips.innerHTML = "";
    el.obdDtcList.innerHTML = "";

    apiFetch(obdUrl("/api/obd/scan"))
      .then(function (data) {
        renderObdScan(data);
      })
      .catch(function () {
        renderObdScan({ ok: false, message: "Couldn't reach the backend to scan." });
      })
      .then(function () {
        obd.scanInFlight = false;
        el.obdScanBtn.disabled = false;
        el.obdScanBtn.textContent = original;
      });
  }

  function runObdClear() {
    if (obd.clearInFlight || !obd.hasScan) return;
    if (
      !window.confirm(
        "Clear all fault codes and reset the engine light?\n\nCodes will return " +
          "if the fault is still present, and readiness monitors reset (matters " +
          "for an MOT emissions check)."
      )
    ) {
      return;
    }
    obd.clearInFlight = true;
    el.obdClearBtn.disabled = true;
    var original = el.obdClearBtn.textContent;
    el.obdClearBtn.textContent = "Clearing…";

    apiFetch("/api/obd/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        adapter: state.simulate ? null : state.currentAdapterId,
        mode: obdMode(),
        confirm: true
      })
    })
      .then(function (data) {
        el.obdMessage.textContent = (data && data.message) || "";
        if (data && data.ok) runObdScan();
      })
      .catch(function () {
        el.obdMessage.textContent = "Couldn't reach the backend to clear codes.";
      })
      .then(function () {
        obd.clearInFlight = false;
        el.obdClearBtn.disabled = !obd.hasScan;
        el.obdClearBtn.textContent = original;
      });
  }

  function renderLive(data) {
    var ok = data && data.ok;
    var readings = (data && data.readings) || [];
    el.liveMessage.hidden = !(data && data.message);
    el.liveMessage.textContent = (data && data.message) || "";
    el.liveGrid.hidden = !ok || !readings.length;
    if (!ok) return;
    el.liveGrid.innerHTML = "";
    readings.forEach(function (r) {
      var cell = document.createElement("div");
      cell.className = "live-cell";

      var value = document.createElement("span");
      value.className = "live-value";
      var v = r.value;
      if (typeof v === "number" && !Number.isInteger(v)) v = v.toFixed(1);
      value.textContent = v;

      var unit = document.createElement("span");
      unit.className = "live-unit";
      unit.textContent = r.unit || "";

      var label = document.createElement("span");
      label.className = "live-label";
      label.textContent = r.label;

      var top = document.createElement("div");
      top.className = "live-top";
      top.appendChild(value);
      top.appendChild(unit);

      cell.appendChild(top);
      cell.appendChild(label);
      el.liveGrid.appendChild(cell);
    });
  }

  function liveTick() {
    if (obd.liveInFlight || !obd.liveOn) return;
    obd.liveInFlight = true;
    apiFetch(obdUrl("/api/obd/live"))
      .then(function (data) {
        if (obd.liveOn) renderLive(data);
      })
      .catch(function () {
        if (obd.liveOn) {
          renderLive({ ok: false, message: "Couldn't reach the backend for live data." });
        }
      })
      .then(function () {
        obd.liveInFlight = false;
      });
  }

  function setLive(on) {
    obd.liveOn = on;
    if (obd.liveTimer) {
      clearInterval(obd.liveTimer);
      obd.liveTimer = null;
    }
    if (on) {
      if (!state.simulate && !state.currentAdapterId) {
        el.liveToggle.checked = false;
        obd.liveOn = false;
        el.liveMessage.hidden = false;
        el.liveMessage.textContent =
          "Pick an ELM327-style adapter (top right) first, or turn on Simulate.";
        return;
      }
      el.liveMessage.hidden = true;
      liveTick();
      obd.liveTimer = setInterval(liveTick, 1500);
    } else {
      el.liveGrid.hidden = true;
      el.liveMessage.hidden = true;
    }
  }

  // ---------- BMW deep scan (ENET cable) ----------

  function resetBmwPanel() {
    el.bmwResult.hidden = true;
    el.bmwSimBanner.hidden = true;
    el.bmwChips.innerHTML = "";
    el.bmwModules.innerHTML = "";
    el.bmwMessage.textContent = "";
  }

  function buildModuleRow(m, onClearRequested) {
    var row = document.createElement("div");
    row.className = "bmw-module" + (m.present ? "" : " is-absent");

    var head = document.createElement("div");
    head.className = "bmw-module-head";

    var title = document.createElement("span");
    title.className = "bmw-module-name";
    title.textContent = m.code + " — " + m.name;
    head.appendChild(title);

    var addr = document.createElement("span");
    addr.className = "bmw-module-addr";
    addr.textContent = m.address;
    head.appendChild(addr);

    var tag = document.createElement("span");
    if (!m.present) {
      tag.className = "tag";
      tag.textContent = "not fitted";
    } else if (m.error) {
      tag.className = "tag dtc-tag is-pending";
      tag.textContent = "no fault read";
    } else if (m.dtcs.length) {
      tag.className = "tag dtc-tag is-stored";
      tag.textContent = m.dtcs.length + " fault" + (m.dtcs.length === 1 ? "" : "s");
    } else {
      tag.className = "tag dtc-tag is-good";
      tag.textContent = "OK";
    }
    head.appendChild(tag);

    if (m.present && m.dtcs.length) {
      var clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "btn btn-danger btn-small";
      clearBtn.textContent = "Clear";
      clearBtn.addEventListener("click", function () {
        onClearRequested(m);
      });
      head.appendChild(clearBtn);
    }
    row.appendChild(head);

    if (m.error) {
      var err = document.createElement("p");
      err.className = "dtc-desc";
      err.textContent = m.error;
      row.appendChild(err);
    }

    m.dtcs.forEach(function (d) {
      var card = document.createElement("div");
      card.className = "dtc-card";

      var cardHead = document.createElement("div");
      cardHead.className = "dtc-card-head";

      var code = document.createElement("span");
      code.className = "dtc-code";
      code.textContent = d.hex;
      cardHead.appendChild(code);

      var status = document.createElement("span");
      status.className = "tag dtc-tag is-stored";
      status.textContent = d.code;
      cardHead.appendChild(status);

      card.appendChild(cardHead);

      var desc = document.createElement("p");
      desc.className = "dtc-desc";
      desc.textContent = (d.flags || []).join(", ") || "status 0x" + d.status;
      card.appendChild(desc);

      row.appendChild(card);
    });

    return row;
  }

  function renderBmwScan(data) {
    el.bmwResult.hidden = false;
    el.bmwSimBanner.hidden = !(data && data.simulated);
    el.bmwChips.innerHTML = "";
    el.bmwModules.innerHTML = "";
    el.bmwMessage.textContent = (data && data.message) || "";

    if (!data || !data.ok) return;

    var v = data.vehicle || {};
    if (v.vin) el.bmwChips.appendChild(chip("VIN", v.vin, "is-mono"));
    if (v.protocol) el.bmwChips.appendChild(chip("Protocol", v.protocol));
    if (v.ip) el.bmwChips.appendChild(chip("Car IP", v.ip, "is-mono"));

    (data.modules || []).forEach(function (m) {
      el.bmwModules.appendChild(buildModuleRow(m, function (mm) {
        runBmwClear(mm.address, mm.code + " — " + mm.name);
      }));
    });
  }

  function runBmwScan() {
    if (bmw.scanInFlight) return;
    bmw.scanInFlight = true;
    el.bmwScanBtn.disabled = true;
    var original = el.bmwScanBtn.textContent;
    el.bmwScanBtn.textContent = "Scanning…";
    el.bmwResult.hidden = false;
    el.bmwChips.innerHTML = "";
    el.bmwModules.innerHTML = "";
    el.bmwMessage.textContent =
      "Walking every module through the gateway… (30–60s, absent modules time out quietly)";

    apiFetch("/api/bmw/scan?mode=" + obdMode())
      .then(function (data) {
        renderBmwScan(data);
      })
      .catch(function () {
        renderBmwScan({ ok: false, message: "Couldn't reach the backend to scan." });
      })
      .then(function () {
        bmw.scanInFlight = false;
        el.bmwScanBtn.disabled = false;
        el.bmwScanBtn.textContent = original;
      });
  }

  function runBmwClear(address, label) {
    if (bmw.clearInFlight) return;
    if (
      !window.confirm(
        "Clear the fault memory of " + label + "?\n\nFaults will return if the " +
          "underlying problem is still present."
      )
    ) {
      return;
    }
    bmw.clearInFlight = true;
    apiFetch("/api/bmw/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: obdMode(), confirm: true, address: address })
    })
      .then(function (data) {
        el.bmwMessage.textContent = (data && data.message) || "";
        if (data && data.ok) runBmwScan();
      })
      .catch(function () {
        el.bmwMessage.textContent = "Couldn't reach the backend to clear.";
      })
      .then(function () {
        bmw.clearInFlight = false;
      });
  }

  // ---------- Service resets (per manufacturer) ----------

  function serviceBrandButtons() {
    return {
      bmw: el.serviceBrandBmw,
      mazda: el.serviceBrandMazda,
      peugeot: el.serviceBrandPeugeot
    };
  }

  function selectServiceBrand(brand) {
    service.brand = brand;
    var buttons = serviceBrandButtons();
    Object.keys(buttons).forEach(function (b) {
      buttons[b].classList.toggle("btn-primary", b === brand);
      buttons[b].classList.toggle("btn-secondary", b !== brand);
    });
    loadServiceResets(brand);
  }

  function confidenceTag(confidence) {
    var span = document.createElement("span");
    var cls =
      confidence === "verified" ? "is-good" :
      confidence === "community" ? "is-pending" : "is-stored";
    span.className = "tag dtc-tag " + cls;
    span.textContent = confidence;
    return span;
  }

  function buildServiceRow(r) {
    var row = document.createElement("div");
    row.className = "bmw-module";

    var head = document.createElement("div");
    head.className = "bmw-module-head";

    var title = document.createElement("span");
    title.className = "bmw-module-name";
    title.textContent = r.name;
    head.appendChild(title);
    head.appendChild(confidenceTag(r.confidence));
    row.appendChild(head);

    var desc = document.createElement("p");
    desc.className = "dtc-desc";
    desc.textContent = r.description;
    row.appendChild(desc);

    if (r.models) {
      var models = document.createElement("p");
      models.className = "obd-hint";
      models.textContent = r.models;
      row.appendChild(models);
    }

    if (r.method === "uds") {
      if (r.steps && r.steps.length) {
        var sends = document.createElement("p");
        sends.className = "dtc-desc is-mono";
        sends.textContent = "Sends: " + r.steps.join("  ·  ");
        row.appendChild(sends);
      }
      var runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.className = "btn btn-primary btn-small";
      runBtn.textContent = service.runningId === r.id ? "Running…" : "Run reset";
      runBtn.disabled = service.runningId !== null;
      runBtn.addEventListener("click", function () {
        runServiceReset(r);
      });
      row.appendChild(runBtn);
    } else {
      var steps = document.createElement("ol");
      steps.className = "service-steps";
      (r.procedure || []).forEach(function (step) {
        var li = document.createElement("li");
        li.textContent = step;
        steps.appendChild(li);
      });
      row.appendChild(steps);
    }

    (r.cautions || []).forEach(function (c) {
      var warn = document.createElement("p");
      warn.className = "dtc-desc";
      warn.textContent = "⚠ " + c;
      row.appendChild(warn);
    });

    return row;
  }

  function renderServiceResets(data) {
    el.serviceList.innerHTML = "";
    el.serviceMessage.hidden = true;
    var resets = (data && data.resets) || [];
    if (!resets.length) {
      el.serviceMessage.hidden = false;
      el.serviceMessage.textContent = "No service resets catalogued for this brand yet.";
      return;
    }
    resets.forEach(function (r) {
      el.serviceList.appendChild(buildServiceRow(r));
    });
  }

  function loadServiceResets(brand) {
    service.loading = true;
    el.serviceList.innerHTML = "";
    el.serviceMessage.hidden = false;
    el.serviceMessage.textContent = "Loading " + brand + " resets…";
    apiFetch("/api/" + brand + "/resets")
      .then(function (data) {
        renderServiceResets(data);
      })
      .catch(function () {
        el.serviceMessage.hidden = false;
        el.serviceMessage.textContent = "Couldn't reach the backend for resets.";
      })
      .then(function () {
        service.loading = false;
      });
  }

  function runServiceReset(r) {
    if (service.runningId) return;
    if (!confirm("Run '" + r.name + "'? This sends the reset over the dongle and changes the car.")) {
      return;
    }
    service.runningId = r.id;
    loadServiceResets(service.brand); // re-render to disable buttons
    var body = { reset_id: r.id, mode: obdMode(), confirm: true };
    if (!state.simulate && state.currentAdapterId) body.adapter = state.currentAdapterId;
    apiFetch("/api/" + service.brand + "/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (res) {
        el.serviceMessage.hidden = false;
        el.serviceMessage.textContent = (res && res.message) || "Reset finished.";
      })
      .catch(function () {
        el.serviceMessage.hidden = false;
        el.serviceMessage.textContent = "Couldn't reach the backend to run the reset.";
      })
      .then(function () {
        service.runningId = null;
        loadServiceResets(service.brand);
      });
  }

  // ---------- Mazda module scan (OBD dongle) ----------

  function resetMazdaPanel() {
    el.mazdaResult.hidden = true;
    el.mazdaSimBanner.hidden = true;
    el.mazdaChips.innerHTML = "";
    el.mazdaModules.innerHTML = "";
    el.mazdaMessage.textContent = "";
    el.mazdaExtras.hidden = true;
    el.mazdaExtrasChips.innerHTML = "";
    el.mazdaExtrasMessage.textContent = "";
  }

  function renderMazdaScan(data) {
    el.mazdaResult.hidden = false;
    el.mazdaSimBanner.hidden = !(data && data.simulated);
    el.mazdaChips.innerHTML = "";
    el.mazdaModules.innerHTML = "";
    el.mazdaMessage.textContent = (data && data.message) || "";

    if (!data || !data.ok) return;

    var v = data.vehicle || {};
    if (v.vin) el.mazdaChips.appendChild(chip("VIN", v.vin, "is-mono"));
    if (v.protocol) el.mazdaChips.appendChild(chip("Protocol", v.protocol));
    if (v.adapter) el.mazdaChips.appendChild(chip("Adapter", v.adapter));

    (data.modules || []).forEach(function (m) {
      el.mazdaModules.appendChild(buildModuleRow(m, function (mm) {
        runMazdaClear(mm.address, mm.code + " — " + mm.name);
      }));
    });
  }

  function runMazdaScan() {
    if (mazda.scanInFlight) return;
    mazda.scanInFlight = true;
    el.mazdaScanBtn.disabled = true;
    var original = el.mazdaScanBtn.textContent;
    el.mazdaScanBtn.textContent = "Scanning…";
    el.mazdaResult.hidden = false;
    el.mazdaChips.innerHTML = "";
    el.mazdaModules.innerHTML = "";
    el.mazdaMessage.textContent =
      "Walking the HS-CAN modules through the dongle… (absent addresses time out quietly)";

    apiFetch(obdUrl("/api/mazda/scan"))
      .then(function (data) {
        renderMazdaScan(data);
      })
      .catch(function () {
        renderMazdaScan({ ok: false, message: "Couldn't reach the backend to scan." });
      })
      .then(function () {
        mazda.scanInFlight = false;
        el.mazdaScanBtn.disabled = false;
        el.mazdaScanBtn.textContent = original;
      });
  }

  function runMazdaClear(address, label) {
    if (mazda.clearInFlight) return;
    if (
      !window.confirm(
        "Clear the fault memory of " + label + "?\n\nFaults will return if the " +
          "underlying problem is still present."
      )
    ) {
      return;
    }
    mazda.clearInFlight = true;
    var body = { mode: obdMode(), confirm: true, address: address };
    if (!state.simulate && state.currentAdapterId) body.adapter = state.currentAdapterId;
    apiFetch("/api/mazda/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (data) {
        el.mazdaMessage.textContent = (data && data.message) || "";
        if (data && data.ok) runMazdaScan();
      })
      .catch(function () {
        el.mazdaMessage.textContent = "Couldn't reach the backend to clear.";
      })
      .then(function () {
        mazda.clearInFlight = false;
      });
  }

  function runMazdaReadings(btn, path, busyText) {
    if (mazda.liveInFlight) return;
    mazda.liveInFlight = true;
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "Reading…";
    el.mazdaExtras.hidden = false;
    el.mazdaExtrasChips.innerHTML = "";
    el.mazdaExtrasMessage.textContent = busyText;

    apiFetch(obdUrl(path))
      .then(function (data) {
        el.mazdaExtrasMessage.textContent = (data && data.message) || "";
        el.mazdaExtrasChips.innerHTML = "";
        ((data && data.readings) || []).forEach(function (r) {
          var value = r.unit ? r.value + " " + r.unit : r.value;
          el.mazdaExtrasChips.appendChild(chip(r.name, value));
        });
      })
      .catch(function () {
        el.mazdaExtrasMessage.textContent = "Couldn't reach the backend.";
      })
      .then(function () {
        mazda.liveInFlight = false;
        btn.disabled = false;
        btn.textContent = original;
      });
  }

  function runMazdaLive() {
    runMazdaReadings(
      el.mazdaLiveBtn, "/api/mazda/live",
      "Asking the modules for the Mazda-specific values…"
    );
  }

  function runMazdaSnapshot() {
    runMazdaReadings(
      el.mazdaSnapBtn, "/api/mazda/snapshot",
      "Listening to the car's broadcast frames (~10s)…"
    );
  }

  // ---------- Phone pairing ----------

  function openPairModal() {
    pair.open = true;
    pair.info = null;
    pair.error = "";
    el.pairModal.hidden = false;
    renderPairModal();
    apiFetch("/api/pair/start", { method: "POST" })
      .then(function (data) {
        pair.info = data;
        renderPairModal();
      })
      .catch(function () {
        pair.error = "Couldn't reach the backend to start pairing.";
        renderPairModal();
      });
    loadPairedDevices();
  }

  function closePairModal() {
    pair.open = false;
    el.pairModal.hidden = true;
  }

  function loadPairedDevices() {
    apiFetch("/api/pair/devices")
      .then(function (data) {
        pair.devices = (data && data.devices) || [];
        if (pair.open) renderPairModal();
      })
      .catch(function () {
        pair.devices = [];
        if (pair.open) renderPairModal();
      });
  }

  function revokeDevice(id) {
    if (!window.confirm("Un-pair this phone? It will need a new code to reconnect.")) return;
    apiFetch("/api/pair/devices/" + encodeURIComponent(id), { method: "DELETE" })
      .then(loadPairedDevices)
      .catch(function () {});
  }

  function renderPairModal() {
    el.pairModalBody.innerHTML = "";

    var hint = document.createElement("p");
    hint.className = "wizard-hint";
    hint.textContent =
      "In the Car Harness phone app, tap “Pair with desktop”. The app finds " +
      "this computer on your WiFi automatically — then enter this code:";
    el.pairModalBody.appendChild(hint);

    if (pair.error) {
      var err = document.createElement("p");
      err.className = "wizard-error";
      err.textContent = pair.error;
      el.pairModalBody.appendChild(err);
    } else if (!pair.info) {
      var loading = document.createElement("p");
      loading.className = "wizard-empty";
      loading.textContent = "Generating a pairing code…";
      el.pairModalBody.appendChild(loading);
    } else {
      var codeBox = document.createElement("div");
      codeBox.className = "pair-code";
      codeBox.textContent = pair.info.code;
      el.pairModalBody.appendChild(codeBox);

      var meta = document.createElement("p");
      meta.className = "pair-meta";
      var addrs = (pair.info.addresses || []).join(", ") || "no LAN address found";
      meta.textContent =
        "Valid 5 minutes, one use. If the app can't find this PC, type the " +
        "address in manually: " + addrs + "  (port " + pair.info.port + "). " +
        "Phone and PC must be on the same WiFi.";
      el.pairModalBody.appendChild(meta);
    }

    var section = document.createElement("div");
    section.className = "wizard-section";
    var title = document.createElement("p");
    title.className = "wizard-section-title";
    title.textContent = "Paired phones";
    section.appendChild(title);

    if (pair.devices.length) {
      var list = document.createElement("div");
      list.className = "adapter-list";
      pair.devices.forEach(function (d) {
        var item = document.createElement("div");
        item.className = "adapter-list-item";

        var info = document.createElement("div");
        info.className = "adapter-list-info";
        var name = document.createElement("span");
        name.className = "adapter-list-name";
        name.textContent = d.name;
        info.appendChild(name);
        var meta2 = document.createElement("span");
        meta2.className = "adapter-list-meta";
        meta2.textContent = "Paired " + (d.pairedAt || "").slice(0, 10);
        info.appendChild(meta2);
        item.appendChild(info);

        var del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-danger btn-small";
        del.textContent = "Un-pair";
        del.addEventListener("click", function () {
          revokeDevice(d.id);
        });
        item.appendChild(del);

        list.appendChild(item);
      });
      section.appendChild(list);
    } else {
      var noneMsg = document.createElement("p");
      noneMsg.className = "wizard-empty";
      noneMsg.textContent = "No phones paired yet.";
      section.appendChild(noneMsg);
    }
    el.pairModalBody.appendChild(section);
  }

  // ---------- Auto-refresh ----------

  function startAutoRefresh() {
    stopAutoRefresh();
    state.autoRefreshTimer = setInterval(refreshStatus, AUTO_REFRESH_MS);
  }

  function stopAutoRefresh() {
    if (state.autoRefreshTimer) {
      clearInterval(state.autoRefreshTimer);
      state.autoRefreshTimer = null;
    }
  }

  // ---------- Event wiring ----------

  el.profileSelect.addEventListener("change", function () {
    state.currentProfileId = el.profileSelect.value;
    refreshAll();
  });

  el.simulateToggle.addEventListener("change", function () {
    state.simulate = el.simulateToggle.checked;
    resetObdPanel();
    resetBmwPanel();
    resetMazdaPanel();
    if (obd.liveOn) {
      el.liveToggle.checked = false;
      setLive(false);
    }
    refreshStatus();
  });

  el.autoRefreshToggle.addEventListener("change", function () {
    state.autoRefresh = el.autoRefreshToggle.checked;
    if (state.autoRefresh) {
      startAutoRefresh();
    } else {
      stopAutoRefresh();
    }
  });

  el.testBtn.addEventListener("click", function () {
    if (state.testInFlight) return;
    state.testInFlight = true;
    el.testBtn.disabled = true;
    var originalText = el.testBtn.textContent;
    el.testBtn.textContent = "Testing…";
    refreshStatus().then(function () {
      state.testInFlight = false;
      el.testBtn.disabled = false;
      el.testBtn.textContent = originalText;
    });
  });

  el.importBtn.addEventListener("click", function () {
    el.importFileInput.click();
  });

  el.importFileInput.addEventListener("change", function () {
    var file = el.importFileInput.files && el.importFileInput.files[0];
    el.importFileInput.value = "";
    if (file) importModule(file);
  });

  el.adapterSelect.addEventListener("change", function () {
    state.currentAdapterId = el.adapterSelect.value;
    resetObdPanel();
    resetMazdaPanel(); // the Mazda scan runs through the picked adapter too
    if (obd.liveOn) {
      el.liveToggle.checked = false;
      setLive(false);
    }
    refreshStatus();
  });

  el.obdScanBtn.addEventListener("click", runObdScan);
  el.obdClearBtn.addEventListener("click", runObdClear);
  el.bmwScanBtn.addEventListener("click", runBmwScan);
  el.mazdaScanBtn.addEventListener("click", runMazdaScan);
  el.mazdaLiveBtn.addEventListener("click", runMazdaLive);
  el.mazdaSnapBtn.addEventListener("click", runMazdaSnapshot);
  el.serviceBrandBmw.addEventListener("click", function () { selectServiceBrand("bmw"); });
  el.serviceBrandMazda.addEventListener("click", function () { selectServiceBrand("mazda"); });
  el.serviceBrandPeugeot.addEventListener("click", function () { selectServiceBrand("peugeot"); });
  el.liveToggle.addEventListener("change", function () {
    setLive(el.liveToggle.checked);
  });

  el.pairBtn.addEventListener("click", openPairModal);
  el.pairModalClose.addEventListener("click", closePairModal);
  el.pairModal.addEventListener("click", function (e) {
    if (e.target === el.pairModal) closePairModal();
  });

  el.addAdapterBtn.addEventListener("click", openWizard);
  el.modalClose.addEventListener("click", closeWizard);
  el.wizardCancel.addEventListener("click", closeWizard);
  el.wizardBack.addEventListener("click", wizardGoBack);
  el.wizardNext.addEventListener("click", wizardAdvance);

  // Click on the dimmed overlay (outside the modal box) closes the wizard.
  el.modal.addEventListener("click", function (e) {
    if (e.target === el.modal) closeWizard();
  });

  // Esc closes; Enter advances where sensible (never from a button/select).
  document.addEventListener("keydown", function (e) {
    if (pair.open && e.key === "Escape") {
      e.preventDefault();
      closePairModal();
      return;
    }
    if (!wizard.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeWizard();
    } else if (e.key === "Enter") {
      var tag = e.target && e.target.tagName;
      if (tag === "BUTTON" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (!el.wizardNext.disabled) {
        e.preventDefault();
        wizardAdvance();
      }
    }
  });

  // ---------- Init ----------

  function init() {
    renderStatusOffline();
    renderProfileOptions();
    renderAdapterSelect();
    loadProfiles().then(function () {
      refreshAll();
    });
    loadAdapters();
    selectServiceBrand("bmw");
  }

  init();
})();
