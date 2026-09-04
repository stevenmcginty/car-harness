/* Car Harness — Live Data
   Talks to /api/live/catalog, /api/live/read and /api/obd/freeze. No framework,
   no dependencies, same as the rest of the front end.

   The one design decision worth knowing: reads are SEQUENTIAL, never on a timer.
   Each pass waits for the car to answer before asking again. A setInterval over a
   Bluetooth dongle queues requests faster than an ELM327 can serve them and the
   display drifts further behind the car with every second. Waiting means the
   refresh rate shown on screen is the real one the car is managing. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SPARK_SAMPLES = 60;
  const STARTERS = ["obd.rpm", "obd.speed", "obd.coolant", "obd.load",
                    "obd.throttle", "obd.module_voltage"];

  const state = {
    mode: "real",
    adapter: "",
    brand: "",
    channels: [],           // catalog, as returned
    groups: [],             // group names, catalog order
    selected: new Set(),
    wide: new Set(),        // tiles expanded to double width
    history: new Map(),     // key -> [numbers]
    holds: new Map(),       // key -> {min, max}
    latest: new Map(),      // key -> channel-shaped reading
    streaming: false,
    recording: false,
    samples: [],            // recorded rows
    filterGroup: "",
    query: "",
    view: "tiles",
    lastRate: null,
    tick: 0,
  };

  // ---------------------------------------------------------------- helpers --

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function status(text, tone) {
    $("status-text").textContent = text;
    $("status-dot").dataset.state = tone || "idle";
  }

  function note(text, tone) {
    const el = $("setup-note");
    el.textContent = text;
    if (tone) el.dataset.tone = tone; else delete el.dataset.tone;
  }

  async function api(path, params) {
    const url = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) url.searchParams.set(k, v);
    });
    const res = await fetch(url, { cache: "no-store" });
    let body = null;
    try { body = await res.json(); } catch { /* fall through to the status text */ }
    if (!res.ok) {
      const detail = body && (body.detail || body.message);
      throw new Error(detail || `The backend answered ${res.status}.`);
    }
    return body;
  }

  const storeKey = () => `carharness.live.${state.brand || "generic"}`;

  function saveSelection() {
    try {
      localStorage.setItem(storeKey(), JSON.stringify([...state.selected]));
    } catch { /* private mode — selection just won't persist */ }
  }

  function loadSelection() {
    try {
      const raw = localStorage.getItem(storeKey());
      if (raw) return new Set(JSON.parse(raw));
    } catch { /* ignore */ }
    return null;
  }

  // ------------------------------------------------------------------ setup --

  async function loadPickers() {
    const brandSel = $("brand");
    const adapterSel = $("adapter");
    try {
      const [adapters, profiles] = await Promise.all([
        api("/api/adapters"), api("/api/profiles"),
      ]);
      const list = adapters.adapters || [];
      adapterSel.innerHTML = list.length
        ? list.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")
        : `<option value="">No adapters configured</option>`;
      // The saved default wins over "whatever is first in the list".
      const defaultId = adapters.defaultId || "";
      if (defaultId && list.some((a) => a.id === defaultId)) adapterSel.value = defaultId;
      state.adapter = adapterSel.value;

      brandSel.innerHTML =
        `<option value="">Standard OBD-II only</option>` +
        (profiles.profiles || []).map((p) =>
          `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    } catch (err) {
      note(`Could not reach the backend: ${err.message}`, "bad");
    }
  }

  // ---------------------------------------------------------------- catalog --

  async function detect() {
    stopStream();
    $("detect").disabled = true;
    status("Asking the car…", "busy");
    note(
      state.brand && state.mode === "real"
        ? "Reading the car's supported-PID list, then one manufacturer pass — that " +
          "second part reads the whole DID table for the brand and can take up to a " +
          "minute over Bluetooth. It only happens once."
        : "Reading the car's supported-PID list…"
    );
    try {
      const res = await api("/api/live/catalog", {
        adapter: state.adapter, mode: state.mode, brand: state.brand,
      });
      if (!res.ok) {
        note(res.message || "The car did not answer.", "bad");
        status("No answer", "bad");
        return;
      }
      state.channels = res.channels || [];
      state.groups = res.groups || [];
      state.history.clear();
      state.holds.clear();
      state.latest.clear();

      const available = new Set(state.channels.map((c) => c.key));
      const saved = loadSelection();
      let want = saved ? [...saved].filter((k) => available.has(k)) : [];
      if (!want.length) want = STARTERS.filter((k) => available.has(k));
      if (!want.length) {
        want = state.channels.filter((c) => c.graphable).slice(0, 6).map((c) => c.key);
      }
      state.selected = new Set(want);
      saveSelection();

      state.channels.forEach((c) => state.latest.set(c.key, c));

      note(res.message || "", res.simulated ? "" : "ok");
      status(res.simulated ? "Simulated" : "Ready", res.simulated ? "sim" : "live");
      $("welcome").hidden = true;
      $("watch").hidden = false;
      $("catalog").hidden = false;
      $("freeze").hidden = false;
      $("nav").hidden = false;
      ["stream", "record", "reset-holds"].forEach((id) => { $(id).hidden = false; });
      renderNav();
      renderCatalog();
      renderWatch();
    } catch (err) {
      note(err.message, "bad");
      status("Error", "bad");
    } finally {
      $("detect").disabled = false;
    }
  }

  // -------------------------------------------------------------------- nav --

  function renderNav() {
    const counts = new Map();
    state.channels.forEach((c) => counts.set(c.group, (counts.get(c.group) || 0) + 1));
    const item = (group, label, count) => `
      <button type="button" class="nav-item${state.filterGroup === group ? " is-on" : ""}"
              data-nav="${esc(group)}">
        ${esc(label)}<span class="nav-item__count">${count}</span>
      </button>`;
    $("nav-items").innerHTML =
      item("", "All values", state.channels.length) +
      state.groups.map((g) => item(g, g, counts.get(g) || 0)).join("") +
      `<button type="button" class="nav-item nav-item--freeze" data-nav-freeze>
         Freeze frame
       </button>`;
  }

  function setGroup(group) {
    state.filterGroup = group;
    $("nav-items").querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("is-on", b.dataset.nav === group && !b.hasAttribute("data-nav-freeze")));
    renderCatalog();
    $("catalog").scrollIntoView({ block: "start" });
  }

  // ---------------------------------------------------------------- catalog --

  function visibleChannels() {
    const q = state.query.trim().toLowerCase();
    return state.channels.filter((c) => {
      if (state.filterGroup && c.group !== state.filterGroup) return false;
      if (!q) return true;
      return (c.label + " " + c.source + " " + c.unit).toLowerCase().includes(q);
    });
  }

  function renderCatalog() {
    const shown = visibleChannels();
    const total = state.channels.length;
    const std = state.channels.filter((c) => c.source === "OBD-II").length;
    $("catalog-title").textContent = state.filterGroup || "All values";
    $("catalog-meta").textContent =
      `${total} value${total === 1 ? "" : "s"} on this car — ${std} standard OBD-II` +
      (total - std ? `, ${total - std} manufacturer` : "") +
      (shown.length !== total ? ` · showing ${shown.length}` : "");

    const byGroup = new Map();
    shown.forEach((c) => {
      if (!byGroup.has(c.group)) byGroup.set(c.group, []);
      byGroup.get(c.group).push(c);
    });

    if (!shown.length) {
      $("channel-groups").innerHTML = `<p class="empty">Nothing matches that.</p>`;
      return;
    }

    $("channel-groups").innerHTML = [...byGroup.entries()].map(([group, items]) => {
      const allOn = items.every((c) => state.selected.has(c.key));
      return `
      <div class="group" data-group="${esc(group)}">
        <div class="group__head">
          <h3 class="group__title">${esc(group)}</h3>
          <span class="group__count">${items.length}</span>
          <button type="button" class="group__all" data-all="${esc(group)}">
            ${allOn ? "Clear all" : "Watch all"}
          </button>
        </div>
        <div class="rows">${items.map(rowHtml).join("")}</div>
      </div>`;
    }).join("");
  }

  function rowHtml(c) {
    const on = state.selected.has(c.key);
    const live = state.latest.get(c.key);
    const value = live && live.text ? `${esc(live.text)}${live.unit ? " " + esc(live.unit) : ""}` : "";
    return `
      <button type="button" class="row${on ? " is-on" : ""}" data-key="${esc(c.key)}"
              aria-pressed="${on}">
        <span class="row__tick" aria-hidden="true">
          <svg viewBox="0 0 10 8"><path d="M1 4l2.6 2.6L9 1.2" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <span class="row__body">
          <span class="row__name">${esc(c.label)}</span>
          <span class="row__meta">${esc(c.source)}${c.unit ? " · " + esc(c.unit) : ""}${c.graphable ? "" : " · not graphable"}</span>
        </span>
        <span class="row__value">${value}</span>
      </button>`;
  }

  // ------------------------------------------------------------- watch area --

  function orderedSelection() {
    // Catalog order, so tiles sit in the same order as the list they came from.
    return state.channels.filter((c) => state.selected.has(c.key)).map((c) => c.key);
  }

  function renderWatch() {
    const keys = orderedSelection();
    $("watch-count").textContent = keys.length ? `${keys.length} selected` : "";
    $("watch-empty").hidden = keys.length > 0;
    $("stream").disabled = !keys.length;
    const tiles = state.view === "tiles";
    $("gauges").hidden = !tiles;
    $("wtable").hidden = tiles;
    if (tiles) renderTiles(keys); else renderTable(keys);
    updateCost();
  }

  function renderTiles(keys) {
    $("gauges").innerHTML = keys.map((key) => {
      const c = state.latest.get(key) || { key, label: key, unit: "", source: "" };
      return `
        <article class="gauge${state.wide.has(key) ? " is-wide" : ""}" data-key="${esc(key)}"
                 title="Click to enlarge">
          <button type="button" class="gauge__drop" data-drop="${esc(key)}"
                  aria-label="Stop watching ${esc(c.label)}">&times;</button>
          <p class="gauge__label">${esc(c.label)}</p>
          <p class="gauge__value" data-value>—</p>
          <svg class="gauge__spark" data-spark viewBox="0 0 100 34" preserveAspectRatio="none" aria-hidden="true"></svg>
          <p class="gauge__holds" data-holds></p>
        </article>`;
    }).join("");
    keys.forEach(paint);
  }

  function renderTable(keys) {
    $("wtable").innerHTML = `
      <div class="wrow wrow--head" aria-hidden="true">
        <span>Value</span><span></span><span style="text-align:right">Current</span>
        <span style="text-align:right">Min</span><span style="text-align:right">Max</span><span></span>
      </div>` +
      keys.map((key) => {
        const c = state.latest.get(key) || { key, label: key, unit: "", source: "" };
        return `
        <div class="wrow" data-key="${esc(key)}">
          <span class="wrow__name">${esc(c.label)}<small>${esc(c.source)}</small></span>
          <svg class="wrow__spark" data-spark viewBox="0 0 100 26" preserveAspectRatio="none" aria-hidden="true"></svg>
          <span class="wrow__value" data-value>—</span>
          <span class="wrow__hold" data-min>—</span>
          <span class="wrow__hold" data-max>—</span>
          <button type="button" class="wrow__drop" data-drop="${esc(key)}"
                  aria-label="Stop watching ${esc(c.label)}">&times;</button>
        </div>`;
      }).join("");
    keys.forEach(paint);
  }

  function activeContainer() {
    return state.view === "tiles" ? $("gauges") : $("wtable");
  }

  function paint(key) {
    const card = activeContainer().querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (!card) return;
    const reading = state.latest.get(key);
    const valueEl = card.querySelector("[data-value]");
    const sparkEl = card.querySelector("[data-spark]");
    if (!reading || reading.text === "" || reading.text == null) {
      valueEl.textContent = "—";
      return;
    }

    if (reading.value === null || reading.value === undefined) {
      valueEl.textContent = reading.text;
      valueEl.classList.add("gauge__value--text");
      if (sparkEl) sparkEl.hidden = true;
      return;
    }

    valueEl.classList.remove("gauge__value--text");
    valueEl.innerHTML = state.view === "tiles"
      ? `${esc(reading.text)}${reading.unit ? `<span class="gauge__unit">${esc(reading.unit)}</span>` : ""}`
      : `${esc(reading.text)}${reading.unit ? `<span>${esc(reading.unit)}</span>` : ""}`;

    const hold = state.holds.get(key);
    if (state.view === "tiles") {
      const holdsEl = card.querySelector("[data-holds]");
      holdsEl.innerHTML = hold
        ? `<span>min <b>${trim(hold.min)}</b></span><span>max <b>${trim(hold.max)}</b></span>` : "";
    } else {
      card.querySelector("[data-min]").textContent = hold ? trim(hold.min) : "—";
      card.querySelector("[data-max]").textContent = hold ? trim(hold.max) : "—";
    }

    const series = state.history.get(key) || [];
    if (sparkEl) {
      sparkEl.hidden = series.length < 2;
      if (series.length >= 2) {
        const h = state.view === "tiles" ? 34 : 26;
        sparkEl.setAttribute("viewBox", `0 0 100 ${h}`);
        sparkEl.innerHTML = sparkPath(series, h);
      }
    }
  }

  const trim = (n) => (Math.round(n * 100) / 100).toString();

  function sparkPath(series, h) {
    let lo = Math.min(...series), hi = Math.max(...series);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }   // a flat line still deserves a line
    const step = 100 / (series.length - 1);
    const pad = 2;
    const points = series.map((v, i) =>
      `${(i * step).toFixed(2)},${(h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2)).toFixed(2)}`).join(" ");
    return `<polyline class="spark-line" points="${points}" vector-effect="non-scaling-stroke"/>`;
  }

  function updateCost() {
    const n = state.selected.size;
    const brandCount = [...state.selected].filter((k) => !k.startsWith("obd.")).length;
    const el = $("cost");
    if (state.mode === "sim" || !n) { el.hidden = true; return; }
    // ~0.12s per standard PID over a dongle; a manufacturer pass reads the
    // profile's whole DID table, so it costs the same whether you picked one or ten.
    const secs = n * 0.12 + (brandCount ? 4 : 0);
    if (secs < 2.5) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent =
      `${n} values selected — expect roughly ${secs.toFixed(0)}s per refresh over a Bluetooth dongle` +
      (brandCount
        ? ". Manufacturer values are read as a whole block, so even one of them adds a few seconds."
        : ". Fewer values means a faster gauge.");
  }

  // -------------------------------------------------------------- streaming --

  async function streamLoop() {
    while (state.streaming) {
      const keys = orderedSelection();
      if (!keys.length) { stopStream(); return; }
      const started = performance.now();
      try {
        const res = await api("/api/live/read", {
          adapter: state.adapter, mode: state.mode,
          brand: state.brand, keys: keys.join(","),
        });
        if (!state.streaming) return;
        if (!res.ok) {
          note(res.message || "The car stopped answering.", "bad");
          status("Lost the car", "bad");
          stopStream();
          return;
        }
        absorb(res.readings || []);
        state.lastRate = (performance.now() - started) / 1000;
        state.tick += 1;
        // Sub-second passes read as "0.0s", which looks broken rather than fast.
        const rate = state.lastRate < 0.95
          ? `${Math.max(1, Math.round(state.lastRate * 1000))}ms`
          : `${state.lastRate.toFixed(1)}s`;
        $("rate").textContent =
          `every ${rate} · ${res.readings.length}/${keys.length} answering` +
          (state.recording ? ` · ${state.samples.length} recorded` : "");
        status(res.simulated ? "Simulated" : "Reading", res.simulated ? "sim" : "live");
      } catch (err) {
        if (!state.streaming) return;
        note(err.message, "bad");
        status("Error", "bad");
        stopStream();
        return;
      }
      // A short breath so the UI paints and an ELM327 isn't hammered flat out.
      await new Promise((r) => setTimeout(r, 120));
    }
  }

  function absorb(readings) {
    const row = { t: new Date().toISOString() };
    readings.forEach((r) => {
      state.latest.set(r.key, r);
      if (r.value !== null && r.value !== undefined) {
        const series = state.history.get(r.key) || [];
        series.push(r.value);
        if (series.length > SPARK_SAMPLES) series.shift();
        state.history.set(r.key, series);

        const hold = state.holds.get(r.key);
        if (!hold) state.holds.set(r.key, { min: r.value, max: r.value });
        else { hold.min = Math.min(hold.min, r.value); hold.max = Math.max(hold.max, r.value); }
      }
      row[r.key] = r.text;
    });
    if (state.recording) state.samples.push(row);
    state.selected.forEach(paint);
    // The catalog's right-hand values are nice to have, not worth a full
    // re-render every pass — refresh them every fourth tick.
    if (state.tick % 4 === 0) renderCatalog();
  }

  function startStream() {
    if (state.streaming || !state.selected.size) return;
    state.streaming = true;
    $("stream").textContent = "Stop";
    $("stream").classList.add("is-on");
    streamLoop();
  }

  function stopStream() {
    state.streaming = false;
    $("stream").textContent = "Start";
    $("stream").classList.remove("is-on");
    if (state.tick) status("Paused", "idle");
  }

  // ---------------------------------------------------------------- record --

  function toggleRecord() {
    if (!state.recording) {
      state.recording = true;
      state.samples = [];
      $("record").lastChild.textContent = "Stop & save";
      $("record").classList.add("is-on");
      return;
    }
    state.recording = false;
    $("record").lastChild.textContent = "Record";
    $("record").classList.remove("is-on");
    if (!state.samples.length) {
      note("Nothing was recorded — start the gauges first.", "bad");
      return;
    }
    downloadCsv();
  }

  function downloadCsv() {
    const keys = [...new Set(state.samples.flatMap((r) => Object.keys(r)))].filter((k) => k !== "t");
    const header = ["time", ...keys.map((k) => {
      const c = state.latest.get(k);
      return c ? `${c.label}${c.unit ? ` (${c.unit})` : ""}` : k;
    })];
    const cell = (v) => {
      const s = v === undefined || v === null ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [header.map(cell).join(",")];
    state.samples.forEach((r) => lines.push([r.t, ...keys.map((k) => r[k])].map(cell).join(",")));

    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `live-data-${state.brand || "obd"}-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    note(`Saved ${state.samples.length} samples to CSV.`, "ok");
  }

  // ---------------------------------------------------------- freeze frame --

  async function readFreeze() {
    const btn = $("freeze-btn");
    btn.disabled = true;
    btn.textContent = "Reading…";
    try {
      const res = await api("/api/obd/freeze", { adapter: state.adapter, mode: state.mode });
      const out = $("freeze-out");
      if (!res.ok) {
        out.innerHTML = `<p class="note">${esc(res.message || "The car did not answer.")}</p>`;
        return;
      }
      const trigger = res.trigger
        ? `<div class="trigger">
             <span class="trigger__code">${esc(res.trigger.code)}</span>
             <span class="trigger__desc">${esc(res.trigger.description || "")}</span>
           </div>` : "";
      const cards = (res.readings || []).map((r) => `
        <article class="gauge">
          <p class="gauge__label">${esc(r.label)}</p>
          <p class="gauge__value">${esc(String(r.value))}${r.unit ? `<span class="gauge__unit">${esc(r.unit)}</span>` : ""}</p>
        </article>`).join("");
      out.innerHTML = trigger +
        (cards ? `<div class="gauges">${cards}</div>` : "") +
        `<p class="note" style="margin-top:18px">${esc(res.message || "")}</p>`;
    } catch (err) {
      $("freeze-out").innerHTML = `<p class="note">${esc(err.message)}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Read stored frame";
    }
  }

  // ------------------------------------------------------------------ wire --

  function toggleKey(key) {
    if (state.selected.has(key)) { state.selected.delete(key); state.wide.delete(key); }
    else state.selected.add(key);
    saveSelection();
  }

  function wire() {
    $("adapter").addEventListener("change", (e) => { state.adapter = e.target.value; });
    $("brand").addEventListener("change", (e) => {
      state.brand = e.target.value;
      stopStream();
      const saved = loadSelection();
      if (saved) state.selected = saved;
    });

    document.querySelectorAll(".segmented__opt[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        stopStream();
        state.mode = btn.dataset.mode;
        document.querySelectorAll(".segmented__opt[data-mode]").forEach((b) => {
          const on = b === btn;
          b.classList.toggle("is-on", on);
          b.setAttribute("aria-checked", String(on));
        });
        $("adapter").disabled = state.mode === "sim";
        updateCost();
      });
    });

    document.querySelectorAll(".segmented__opt[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.view = btn.dataset.view;
        try { localStorage.setItem("carharness.live.view", state.view); } catch { /* ok */ }
        document.querySelectorAll(".segmented__opt[data-view]").forEach((b) => {
          const on = b === btn;
          b.classList.toggle("is-on", on);
          b.setAttribute("aria-checked", String(on));
        });
        renderWatch();
      });
    });

    $("detect").addEventListener("click", detect);
    $("stream").addEventListener("click", () => (state.streaming ? stopStream() : startStream()));
    $("record").addEventListener("click", toggleRecord);
    $("freeze-btn").addEventListener("click", readFreeze);

    $("reset-holds").addEventListener("click", () => {
      state.holds.clear();
      state.history.clear();
      state.selected.forEach(paint);
    });

    $("nav-items").addEventListener("click", (e) => {
      const item = e.target.closest(".nav-item");
      if (!item) return;
      if (item.hasAttribute("data-nav-freeze")) {
        $("freeze").scrollIntoView({ block: "start" });
        return;
      }
      setGroup(item.dataset.nav);
    });

    $("channel-groups").addEventListener("click", (e) => {
      const all = e.target.closest(".group__all");
      if (all) {
        const items = visibleChannels().filter((c) => c.group === all.dataset.all);
        const allOn = items.every((c) => state.selected.has(c.key));
        items.forEach((c) => {
          if (allOn) { state.selected.delete(c.key); state.wide.delete(c.key); }
          else state.selected.add(c.key);
        });
        saveSelection();
        renderCatalog();
        renderWatch();
        return;
      }
      const row = e.target.closest(".row");
      if (!row) return;
      toggleKey(row.dataset.key);
      row.classList.toggle("is-on", state.selected.has(row.dataset.key));
      row.setAttribute("aria-pressed", String(state.selected.has(row.dataset.key)));
      renderWatch();
    });

    const watchClick = (e) => {
      const btn = e.target.closest("[data-drop]");
      if (btn) {
        state.selected.delete(btn.dataset.drop);
        state.wide.delete(btn.dataset.drop);
        saveSelection();
        renderWatch();
        renderCatalog();
        return;
      }
      const card = e.target.closest(".gauge[data-key]");
      if (card) {
        // A tap on the tile itself toggles the big double-width view.
        const key = card.dataset.key;
        if (state.wide.has(key)) state.wide.delete(key); else state.wide.add(key);
        card.classList.toggle("is-wide", state.wide.has(key));
        paint(key);
      }
    };
    $("gauges").addEventListener("click", watchClick);
    $("wtable").addEventListener("click", watchClick);

    let searchTimer = null;
    $("search").addEventListener("input", (e) => {
      clearTimeout(searchTimer);
      const value = e.target.value;
      searchTimer = setTimeout(() => { state.query = value; renderCatalog(); }, 120);
    });

    // Streaming to a screen nobody is looking at just wears the dongle out.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && state.streaming) stopStream();
    });
  }

  // --------------------------------------------------------------- reveals --

  function reveals() {
    const items = document.querySelectorAll("[data-reveal]");
    if (!("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("is-revealed"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-revealed");
        io.unobserve(entry.target);      // never re-animate on scroll-back
      });
    }, { rootMargin: "0px 0px -10% 0px" });
    items.forEach((el) => io.observe(el));
  }

  /* Deep link, so a setup can be bookmarked and opened straight into:
     /live.html?mode=sim&brand=mazda&go=1
     Everything it does is read-only, which is why "go" is allowed to start on
     its own — it cannot change anything on the car. */
  async function applyDeepLink() {
    const p = new URLSearchParams(location.search);

    const mode = p.get("mode");
    if (mode === "sim" || mode === "real") {
      const btn = document.querySelector(`.segmented__opt[data-mode="${mode}"]`);
      if (btn && !btn.classList.contains("is-on")) btn.click();
    }

    const adapter = p.get("adapter");
    if (adapter && [...$("adapter").options].some((o) => o.value === adapter)) {
      $("adapter").value = adapter;
      state.adapter = adapter;
    }

    const brand = p.get("brand");
    if (brand !== null && [...$("brand").options].some((o) => o.value === brand)) {
      $("brand").value = brand;
      state.brand = brand;
      const saved = loadSelection();
      if (saved) state.selected = saved;
    }

    if (p.get("go") === "1") {
      await detect();
      if (state.channels.length) startStream();
    }
  }

  try {
    const view = localStorage.getItem("carharness.live.view");
    if (view === "list" || view === "tiles") {
      state.view = view;
      document.querySelectorAll(".segmented__opt[data-view]").forEach((b) => {
        const on = b.dataset.view === view;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-checked", String(on));
      });
    }
  } catch { /* ok */ }

  wire();
  reveals();
  loadPickers().then(applyDeepLink);
})();
