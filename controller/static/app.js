"use strict";
// Apply the saved theme before CSS paints, without inline scripts or external assets.
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
let themePreference = null;
try {
  const saved = localStorage.getItem("protonnode-theme");
  if (saved === "dark" || saved === "light") themePreference = saved;
} catch (_) {}
function applyTheme() {
  const dark =
    (themePreference || (systemTheme.matches ? "dark" : "light")) === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    const label = `Switch to ${dark ? "light" : "dark"} mode`;
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
    toggle.setAttribute("aria-pressed", String(dark));
    toggle.querySelector(".theme-label").textContent = dark
      ? "Light mode"
      : "Dark mode";
  }
}
applyTheme();
systemTheme.addEventListener("change", () => {
  if (!themePreference) applyTheme();
});
window.addEventListener("storage", (event) => {
  if (event.key === "protonnode-theme" || event.key === null) {
    themePreference =
      event.newValue === "light" || event.newValue === "dark"
        ? event.newValue
        : null;
    applyTheme();
  }
});
document.addEventListener("DOMContentLoaded", () => {
  applyTheme();
  document.getElementById("theme-toggle").addEventListener("click", () => {
    themePreference =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem("protonnode-theme", themePreference);
    } catch (_) {}
    applyTheme();
  });
  const $ = (id) => document.getElementById(id);
  const state = {
    key: "",
    servers: [],
    status: null,
    selected: null,
    page: 0,
    busy: false,
    pending: false,
    favorites: new Set(),
    lastNotice: null,
  };
  const speed = { running: false, raw: null, stage: "" };
  const pageSize = 6;
  const countryCodes = {
    "United States": "US",
    "United Kingdom": "GB",
    Switzerland: "CH",
    Netherlands: "NL",
    Germany: "DE",
    France: "FR",
    Canada: "CA",
    Australia: "AU",
    Japan: "JP",
    Singapore: "SG",
    Sweden: "SE",
    Norway: "NO",
    Finland: "FI",
    Denmark: "DK",
    Spain: "ES",
    Italy: "IT",
    Poland: "PL",
    Austria: "AT",
    Belgium: "BE",
    Ireland: "IE",
    Portugal: "PT",
    Brazil: "BR",
    Mexico: "MX",
    India: "IN",
    "South Korea": "KR",
    "Hong Kong": "HK",
    Taiwan: "TW",
    "New Zealand": "NZ",
    "South Africa": "ZA",
    Romania: "RO",
    "Czech Republic": "CZ",
    Iceland: "IS",
    Luxembourg: "LU",
    Israel: "IL",
    Ukraine: "UA",
    Hungary: "HU",
    Greece: "GR",
  };
  function countryIcon(country) {
    const code = countryCodes[country];
    return code ? code : '<svg class="icon"><use href="#i-globe"/></svg>';
  }
  function updateControls() {
    const data = state.status;
    const locked = state.busy || state.pending || speed.running || !data?.ready;
    $("connect").disabled = locked || !state.selected;
    $("reconnect").disabled = locked || !data?.saved;
    $("disconnect").disabled =
      locked || !data?.saved || data?.connection === "disconnected";
  }
  function controllerReachable(reachable) {
    $("live-dot").className = `status-dot ${reachable ? "" : "offline"}`;
    $("live-label").textContent = reachable
      ? "Updates every 4 seconds"
      : "Updates interrupted";
  }
  try {
    state.favorites = new Set(
      JSON.parse(localStorage.getItem("protonnode-favorites") || "[]"),
    );
  } catch (_) {}
  const escapeHTML = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  let noticeTimer;
  function notice(message, error = false) {
    clearTimeout(noticeTimer);
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
    if (message && !error)
      noticeTimer = setTimeout(() => {
        $("notice").hidden = true;
      }, 8000);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      cache: "no-store",
      signal: AbortSignal.timeout(30000),
      headers: {
        "Content-Type": "application/json",
        "X-ProtonNode-Request": "1",
        ...(state.key ? { "X-API-Key": state.key } : {}),
        ...options.headers,
      },
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        $("auth").hidden = false;
        $("identity").textContent = "Authentication required";
      }
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    $("auth").hidden = true;
    return data;
  }
  function filtered() {
    const term = $("search").value.toLowerCase().trim(),
      country = $("country").value,
      mode = $("mode").value;
    return state.servers.filter(
      (s) =>
        (!country || s.country === country) &&
        (mode === "any" || s[mode]) &&
        (!$("favorites-only").checked || state.favorites.has(s.hostname)) &&
        (!term ||
          [s.name, s.city, s.country, s.hostname]
            .join(" ")
            .toLowerCase()
            .includes(term)),
    );
  }
  function renderServers() {
    const all = filtered(),
      total = Math.max(1, Math.ceil(all.length / pageSize));
    state.page = Math.min(state.page, total - 1);
    if (state.selected && !all.some((s) => s.hostname === state.selected))
      state.selected = null;
    $("count").textContent =
      `${all.length.toLocaleString()} matching ${all.length === 1 ? "server" : "servers"}`;
    $("page").textContent = all.length
      ? `${state.page * pageSize + 1}–${Math.min((state.page + 1) * pageSize, all.length)} of ${all.length.toLocaleString()}`
      : "0 servers";
    $("previous").disabled = state.page === 0;
    $("next").disabled = state.page >= total - 1;
    const markup = all.length
      ? all
          .slice(state.page * pageSize, state.page * pageSize + pageSize)
          .map(
            (s) =>
              `<div class="server-row ${state.selected === s.hostname ? "selected" : ""}" data-server="${escapeHTML(s.hostname)}"><input type="radio" name="server" aria-label="${escapeHTML(s.name + " in " + s.city + ", " + s.country)}" ${state.selected === s.hostname ? "checked" : ""}><span class="country-icon" aria-hidden="true">${countryIcon(s.country)}</span><div class="server-main"><span class="server-name">${escapeHTML(s.city || s.country)}${state.status?.connection === "connected" && state.status?.server?.hostname === s.hostname ? '<span class="current-tag">Current</span>' : ""}</span><span class="server-detail">${escapeHTML(s.country)} · ${escapeHTML(s.name)}</span></div><span class="server-cap">${s.tor ? "Tor + VPN" : s.p2p ? "P2P" : "Standard"}</span><button class="favorite quiet ${state.favorites.has(s.hostname) ? "saved" : ""}" data-favorite="${escapeHTML(s.hostname)}" aria-label="${state.favorites.has(s.hostname) ? "Remove favorite" : "Add favorite"} ${escapeHTML(s.name)}" aria-pressed="${state.favorites.has(s.hostname)}">${state.favorites.has(s.hostname) ? "★" : "☆"}</button></div>`,
          )
          .join("")
      : `<p class="empty">${$("favorites-only").checked ? "No saved servers match these filters. Save a server with its star, or turn off Favorites only." : "No matching servers. Try another country, route type, or search."}</p>`;
    if (state.listMarkup !== markup) {
      const active = document.activeElement,
        row = active?.closest("[data-server]"),
        focusHost = row?.dataset.server,
        isFavorite = !!active?.dataset.favorite;
      $("server-list").innerHTML = markup;
      state.listMarkup = markup;
      if (focusHost)
        for (const replacement of $("server-list").querySelectorAll(
          "[data-server]",
        ))
          if (replacement.dataset.server === focusHost)
            replacement
              .querySelector(isFavorite ? "button" : "input")
              .focus({ preventScroll: true });
    }
    const selected = state.servers.find((s) => s.hostname === state.selected);
    $("selected-name").textContent = selected
      ? `${selected.city || selected.country} · ${selected.name}`
      : "Choose a destination";
    $("selected-detail").textContent = selected
      ? selected.hostname
      : "Select a server above to change your route.";
    updateControls();
  }
  function renderStatus(data) {
    state.status = data;
    $("demo").hidden = !data.demo;
    $("identity").textContent = data.identity;
    const active = data.operations.find((o) =>
      ["queued", "running"].includes(o.state),
    );
    state.busy = !!active || speed.running || state.pending;
    controllerReachable(true);
    $("state").textContent =
      active?.request.action === "speedtest"
        ? "Speed test running"
        : active
          ? "Changing route"
          : data.connection === "connected"
            ? "Connected"
            : data.connection === "disconnected"
              ? "Disconnected"
              : "Connection unavailable";
    $("state").className = `badge ${active ? "working" : data.connection}`;
    const connected = data.connection === "connected";
    $("route-path").classList.toggle("inactive", !connected);
    $("route-location").textContent =
      connected && data.server
        ? data.server.city || data.server.country
        : "No active tunnel";
    $("tunnel-health").textContent =
      active && active.request.action !== "speedtest"
        ? "Changing route"
        : connected && data.probe.healthy
          ? "Healthy"
          : data.connection === "disconnected"
            ? "Disconnected"
            : "Unverified";
    $("tunnel-health").classList.toggle("warning", !connected);
    $("copy-ip").disabled = !data.probe.public_ip;
    $("connection-title").textContent = data.server
      ? `${data.server.city || data.server.country}, ${data.server.country}`.replace(
          /^(.+), \1$/,
          "$1",
        )
      : "No active route";
    $("current-server").textContent = data.server
      ? `${data.server.name} · ${data.server.hostname}`
      : "Choose a server below to connect";
    $("public-ip").textContent = data.probe.public_ip || "—";
    $("checked").textContent = data.probe.checked_at
      ? `Internet checked ${new Date(data.probe.checked_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
      : "No verified internet connection";
    $("saved-summary").textContent = data.saved
      ? "Selection saved for restarts"
      : "No saved selection yet";
    $("progress").hidden = !active && data.ready;
    $("progress-title").textContent =
      active?.stage || "Restoring saved connection";
    $("progress-detail").textContent =
      active?.request.action === "speedtest"
        ? "Measuring raw Proton on the gateway."
        : active?.error
          ? `${active.error}. Attempting recovery…`
          : "Tailnet access stays available. This page can be closed safely.";
    updateControls();
    $("reconnect").textContent =
      data.connection === "disconnected" ? "Connect saved" : "Reconnect";

    $("history").innerHTML = data.operations.length
      ? data.operations
          .slice(0, 5)
          .map(
            (o) =>
              `<div class="activity ${escapeHTML(o.state)}"><div class="activity-title"><i></i><strong>${escapeHTML(o.request.action === "speedtest" ? "Proton speed test" : o.request.action === "switch" ? o.result?.server?.name || o.request.selection?.hostname || o.request.selection?.city || "Server change" : o.request.action[0].toUpperCase() + o.request.action.slice(1))}</strong></div><p>${escapeHTML(o.error || o.stage)}</p><time>${escapeHTML(new Date(o.created * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))} · ${escapeHTML(o.state)}</time></div>`,
          )
          .join("")
      : '<p class="muted">Your connection changes will appear here.</p>';
    const latest = data.operations[0];
    if (
      latest &&
      ["failed", "succeeded", "interrupted"].includes(latest.state) &&
      latest.id !== state.lastNotice
    ) {
      state.lastNotice = latest.id;
      if (latest.state === "failed")
        notice(`${latest.error}. ${latest.stage}.`, true);
    }
    renderServers();
    renderSpeed(data, active);
  }
  async function loadCatalog() {
    const data = await api("/api/v1/servers");
    state.servers = data.servers;
    const selected = $("country").value;
    $("country").innerHTML =
      '<option value="">All countries</option>' +
      [...new Set(state.servers.map((s) => s.country))]
        .sort()
        .map(
          (c) => `<option value="${escapeHTML(c)}">${escapeHTML(c)}</option>`,
        )
        .join("");
    $("country").value = selected;
    renderServers();
  }
  async function poll() {
    try {
      renderStatus(await api("/api/v1/status"));
      if (state.pollError && $("notice").textContent === state.pollError)
        notice("");
      state.pollError = null;
    } catch (error) {
      state.pollError = error.message;
      notice(error.message, true);
      controllerReachable(false);
      $("state").textContent = "Controller unavailable";
      $("state").className = "badge unavailable";
      $("tunnel-health").textContent = "Unverified";
      $("tunnel-health").classList.add("warning");
      $("route-path").classList.add("inactive");
      if (state.status) state.status = { ...state.status, ready: false };
      updateControls();
      renderSpeed();
    }
  }
  async function operate(action, selection) {
    if (state.busy || state.pending || !state.status?.ready) return;
    state.pending = true;
    state.busy = true;
    updateControls();
    renderSpeed();
    try {
      const body = { action };
      if (selection) body.selection = selection;
      // Each user gesture gets one key; a network retry reuses the same request.
      const key =
        globalThis.crypto?.randomUUID?.() ||
        `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const options = {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify(body),
      };
      let operation;
      try {
        operation = await api("/api/v1/operations", options);
      } catch (error) {
        if (error instanceof TypeError)
          operation = await api("/api/v1/operations", options);
        else throw error;
      }
      notice(
        "Connection change accepted. You can leave this page and return to check progress.",
      );
      $("progress").hidden = false;
      $("progress-title").textContent = operation.stage;
      await poll();
    } catch (error) {
      notice(error.message, true);
    } finally {
      state.pending = false;
      state.busy = !!state.status?.operations?.some((o) =>
        ["queued", "running"].includes(o.state),
      );
      updateControls();
      renderSpeed();
    }
  }
  $("server-list").addEventListener("click", (event) => {
    const favorite = event.target.closest("[data-favorite]");
    if (favorite) {
      const name = favorite.dataset.favorite;
      state.favorites.has(name)
        ? state.favorites.delete(name)
        : state.favorites.add(name);
      try {
        localStorage.setItem(
          "protonnode-favorites",
          JSON.stringify([...state.favorites]),
        );
      } catch (_) {}
      renderServers();
      return;
    }
    const row = event.target.closest("[data-server]");
    if (row) {
      state.selected = row.dataset.server;
      renderServers();
    }
  });
  $("server-list").addEventListener("change", (event) => {
    if (event.target.matches("input[type=radio]")) {
      state.selected = event.target.closest("[data-server]").dataset.server;
      renderServers();
    }
  });
  for (const id of ["search", "country", "mode", "favorites-only"])
    $(id).addEventListener("input", () => {
      state.page = 0;
      renderServers();
    });
  $("previous").onclick = () => {
    state.page--;
    renderServers();
  };
  $("next").onclick = () => {
    state.page++;
    renderServers();
  };
  $("connect").onclick = () =>
    operate("switch", { hostname: state.selected, mode: $("mode").value });
  $("reconnect").onclick = () =>
    operate(
      state.status?.connection === "disconnected" ? "connect" : "reconnect",
    );
  $("disconnect").onclick = () => operate("disconnect");
  $("refresh").onclick = async () => {
    $("refresh").disabled = true;
    try {
      await loadCatalog();
      notice("Server list refreshed.");
    } catch (error) {
      notice(error.message, true);
    } finally {
      $("refresh").disabled = false;
    }
  };
  $("copy-ip").onclick = async () => {
    const ip = state.status?.probe.public_ip;
    if (!ip) return;
    try {
      await navigator.clipboard.writeText(ip);
      notice("Public IP copied.");
    } catch (_) {
      notice(
        "Copy is unavailable in this browser. Select the public IP to copy it.",
        true,
      );
    }
  };
  $("auth-form").onsubmit = async (event) => {
    event.preventDefault();
    state.key = $("api-key").value.trim();
    $("api-key").value = "";
    notice("");
    await initialize();
  };
  const example = `curl '${location.origin}/api/v1/operations' \\\n  -H 'Authorization: Bearer YOUR_CONTROL_KEY' \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-ProtonNode-Request: 1' \\\n  -H 'Idempotency-Key: miami-switch-001' \\\n  --data '{"action":"switch","selection":{"country":"United States","city":"Miami","mode":"p2p"}}'`;
  $("api-example").textContent = example;
  $("copy-api").onclick = async () => {
    try {
      await navigator.clipboard.writeText(example);
      $("copy-result").textContent = "Copied";
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents($("api-example"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      $("copy-result").textContent = "Select and copy the example below.";
    }
  };
  function formatSpeed(value) {
    return Number.isFinite(value) && value >= 0
      ? value.toLocaleString(undefined, { maximumFractionDigits: 1 })
      : "—";
  }
  function speedTime(value) {
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  function speedMessage(text, error = false) {
    $("speed-message").textContent = text;
    $("speed-message").classList.toggle("error", error);
  }
  function renderSpeed(
    data = state.status,
    active = data?.operations?.find((o) =>
      ["queued", "running"].includes(o.state),
    ),
  ) {
    const recent = data?.last_speedtest?.result;
    if (recent && (!speed.raw || recent.measured_at >= speed.raw.measured_at))
      speed.raw = recent;
    for (const [prefix, result] of [["raw", speed.raw]]) {
      $(prefix + "-down").textContent = formatSpeed(
        result?.download_median_mbps,
      );
      $(prefix + "-up").textContent = formatSpeed(result?.upload_median_mbps);
    }
    $("raw-context").textContent = speed.raw
      ? `Last result · ${speed.raw.server.name} · ${speed.raw.server.city} · ${speedTime(speed.raw.measured_at)} · ${speed.raw.public_ip}`
      : "No test yet. Run a test to measure this route.";
    const recentTest = data?.operations?.find(
      (o) =>
        o.request.action === "speedtest" &&
        ["succeeded", "failed", "interrupted"].includes(o.state),
    );
    const cooldown = recentTest
      ? Math.max(0, Math.ceil(30 - (Date.now() / 1000 - recentTest.updated)))
      : 0;
    $("run-speed").disabled =
      speed.running ||
      state.pending ||
      !!active ||
      !!cooldown ||
      !data?.ready ||
      data?.connection !== "connected";
    $("run-speed").textContent =
      speed.running || active?.request.action === "speedtest"
        ? "Testing…"
        : cooldown
          ? `Run again in ${cooldown}s`
          : "Run speed test";
    $("speed-data-cost").textContent = "About 210 MB on the server";
    const running = speed.running || active?.request.action === "speedtest";
    $("speed-progress").hidden = !running;
    $("speed-stage").textContent =
      active?.request.action === "speedtest"
        ? active.stage
        : speed.stage || "Preparing the speed test";
  }
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function runSpeedTests() {
    if (speed.running || state.busy || state.pending || !state.status?.ready)
      return;
    speed.running = true;
    speed.stage = "Starting the Proton test";
    state.busy = true;
    renderSpeed();
    renderServers();
    speedMessage(
      "Tests run only after this click. The gateway test can finish even if this page is closed.",
    );
    try {
      const key =
        globalThis.crypto?.randomUUID?.() ||
        `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let operation = await api("/api/v1/operations", {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify({ action: "speedtest" }),
      });
      const deadline = performance.now() + 240000;
      while (
        !["succeeded", "failed", "interrupted"].includes(operation.state)
      ) {
        speed.stage = operation.stage;
        renderSpeed();
        if (performance.now() > deadline)
          throw new Error(
            "The gateway test is still running. Its result will appear when it finishes; it will not be restarted automatically.",
          );
        await pause(1200);
        operation = await api("/api/v1/operations/" + operation.id);
      }
      if (operation.state !== "succeeded")
        throw new Error(operation.error || operation.stage);
      speed.raw = operation.result;
      renderSpeed();
      speedMessage("Raw Proton measurement complete.");
    } catch (error) {
      speedMessage(error.message, true);
    } finally {
      speed.running = false;
      speed.stage = "";
      await poll();
      renderSpeed();
    }
  }
  $("run-speed").addEventListener("click", runSpeedTests);

  async function initialize() {
    try {
      await loadCatalog();
      await poll();
    } catch (error) {
      notice(error.message, true);
      controllerReachable(false);
    }
  }
  initialize();
  let polling = false;
  setInterval(async () => {
    if (document.hidden || polling) return;
    polling = true;
    try {
      await poll();
    } finally {
      polling = false;
    }
  }, 4000);
});
