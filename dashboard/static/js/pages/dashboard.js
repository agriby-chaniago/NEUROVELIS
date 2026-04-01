/**
 * dashboard.js — NEUROSENSE real-time dashboard
 * Consumes SSE stream from /stream and updates Chart.js charts + metric cards.
 */

"use strict";

// ── Configuration ─────────────────────────────────────────────────────────
const MAX_POINTS = 60; // rolling window (60 data points ≈ 60 s at 1 Hz)
const RECONNECT_MS = 3000; // reconnect delay after SSE error

const elCache = new Map();

function byId(id) {
  if (!elCache.has(id)) {
    elCache.set(id, document.getElementById(id) || null);
  }
  return elCache.get(id);
}

// ── Chart defaults ────────────────────────────────────────────────────────
Chart.defaults.color = "#52697e";
Chart.defaults.borderColor = "#d0d9e4";
Chart.defaults.font.family = '"IBM Plex Sans", "Segoe UI", Roboto, sans-serif';
Chart.defaults.font.size = 11;

const warmupUI = window.NeurosenseWarmupUI || null;

const CHART_OPTIONS = (yLabel, suggestedMin, suggestedMax) => ({
  animation: false,
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  plugins: {
    legend: { labels: { boxWidth: 10, padding: 10, font: { size: 10 } } },
    tooltip: {
      backgroundColor: "#ffffff",
      titleColor: "#1a2b3c",
      bodyColor: "#52697e",
      borderColor: "#d0d9e4",
      borderWidth: 1,
    },
  },
  scales: {
    x: {
      grid: { color: "#e8edf3" },
      ticks: { maxTicksLimit: 6, font: { size: 9 } },
    },
    y: {
      grid: { color: "#e8edf3" },
      title: { display: !!yLabel, text: yLabel, font: { size: 9 } },
      ticks: { font: { size: 9 } },
      suggestedMin,
      suggestedMax,
    },
  },
});

// ── Shared label buffer (timestamps) ─────────────────────────────────────
const labels = [];

function pushLabel(ts) {
  const d = ts ? new Date(ts) : new Date();
  const formatted = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  labels.push(formatted);
  if (labels.length > MAX_POINTS) labels.shift();
}

function makeDataset(label, color, data) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color + "22",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0.3,
    fill: false,
  };
}

function makeBuffer() {
  return [];
}

function pushVal(buf, val) {
  buf.push(val !== null && val !== undefined ? val : NaN);
  if (buf.length > MAX_POINTS) buf.shift();
}

// ── Data buffers ──────────────────────────────────────────────────────────
const buf = {
  hr: makeBuffer(),
  spo2: makeBuffer(),
  temp: makeBuffer(),
  hum: makeBuffer(),
  pres: makeBuffer(),
  gsr: makeBuffer(),
  ads: makeBuffer(),
};

// ── Chart: Heart Rate & SpO2 ─────────────────────────────────────────────
const chartHR = new Chart(document.getElementById("chart-hr"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Heart Rate (BPM)", "#b71c1c", buf.hr),
      makeDataset("SpO\u2082 (%)", "#1a5fad", buf.spo2),
    ],
  },
  options: CHART_OPTIONS("", 40, 105),
});

// ── Chart: Temperature & Humidity ──────────────────────────────────────
const chartEnv = new Chart(document.getElementById("chart-env"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Temperature (\u00b0C)", "#c25d00", buf.temp),
      makeDataset("Humidity (%RH)", "#1e7845", buf.hum),
    ],
  },
  options: CHART_OPTIONS("", 0, 100),
});

// ── Chart: Pressure ───────────────────────────────────────────────────────
const chartPres = new Chart(document.getElementById("chart-pres"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Pressure (hPa)", "#5e35b1", buf.pres)],
  },
  options: CHART_OPTIONS("hPa", 950, 1060),
});

// ── Chart: GSR / EDA ──────────────────────────────────────────────────────
const chartGSR = new Chart(document.getElementById("chart-gsr"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Conductance (µS)", "#0277bd", buf.gsr)],
  },
  options: CHART_OPTIONS("µS", 0, 50),
});

// ── Metric card helpers ───────────────────────────────────────────────────
function setMetric(id, value, decimals = 1, valid = true) {
  const el = byId(id);
  if (!el) return;
  el.textContent =
    value !== null && value !== undefined
      ? Number(value).toFixed(decimals)
      : "—";
  const card = el.closest(".metric-card");
  if (card) {
    card.classList.toggle("invalid", !valid);
  }
}

function updateCards(d) {
  setMetric("val-hr", d.heart_rate_bpm, 0, d.hr_valid);
  setMetric("val-spo2", d.spo2_percent, 1, d.spo2_valid);
  setMetric("val-temp", d.temperature_celsius, 1, true);
  setMetric(
    "val-hum",
    d.humidity_percent,
    1,
    d.humidity_percent !== null && d.humidity_percent !== undefined,
  );
  setMetric("val-pres", d.pressure_hpa, 1, true);
  setMetric("val-gsr", d.gsr_conductance_us, 4, true);
  setMetric(
    "val-ads",
    d.ads1_ch0_V,
    4,
    d.ads1_ch0_V !== null && d.ads1_ch0_V !== undefined,
  );
}

// ── Alert banner ──────────────────────────────────────
const alertBanner = byId("alert-banner");
const alertText = byId("alert-text");

function updateAlert(d) {
  if (!alertBanner || !alertText) return;
  if (d.alert_active) {
    alertText.textContent =
      "PERINGATAN: " + (d.alert_reasons || "kondisi bahaya terdeteksi");
    alertBanner.classList.add("visible");
  } else {
    alertBanner.classList.remove("visible");
  }
}

// ── SSE connection ────────────────────────────────────────────────────────
const statusDot = byId("status-dot");
const lastUpdate = byId("last-update");
const footerTs = byId("footer-ts");
const sseClient = window.NeurosenseSSEClient || null;
const chartUtils = window.NeurosenseChartUtils || null;
let dashboardStream = null;
let unregisterChartResize = null;
let pendingRealtimeEvent = null;
let realtimeFlushRafId = null;
let dashboardGuardrailLevel = 0;
let slowFrameStreak = 0;
let stableFrameStreak = 0;
let chartUpdateCycle = 0;

const metricCards = Array.from(document.querySelectorAll(".metric-card"));
const diskWarn = byId("disk-warning");
const diskText = byId("disk-warning-text");
const fpsBadge = byId("camera-fps");

function updateFrameGuardrail(frameCostMs) {
  if (!Number.isFinite(frameCostMs)) return;
  if (frameCostMs > 18) {
    slowFrameStreak += 1;
    stableFrameStreak = 0;
  } else {
    stableFrameStreak += 1;
    slowFrameStreak = 0;
  }

  if (slowFrameStreak >= 4 && dashboardGuardrailLevel < 2) {
    dashboardGuardrailLevel += 1;
    slowFrameStreak = 0;
  }
  if (stableFrameStreak >= 40 && dashboardGuardrailLevel > 0) {
    dashboardGuardrailLevel -= 1;
    stableFrameStreak = 0;
  }
}

function shouldUpdateChart(index) {
  if (dashboardGuardrailLevel === 0) return true;
  if (dashboardGuardrailLevel === 1) {
    const phase = chartUpdateCycle % 2;
    return phase === 0 ? index < 2 : index >= 2;
  }
  const phase = chartUpdateCycle % 3;
  if (phase === 0) return index < 2;
  if (phase === 1) return index === 2;
  return index === 3;
}

function updateChartsWithGuardrail(d) {
  pushLabel(d.timestamp_utc);
  pushVal(buf.hr, d.heart_rate_bpm);
  pushVal(buf.spo2, d.spo2_percent);
  pushVal(buf.temp, d.temperature_celsius);
  pushVal(buf.hum, d.humidity_percent);
  pushVal(buf.pres, d.pressure_hpa);
  pushVal(buf.gsr, d.gsr_conductance_us);
  pushVal(buf.ads, d.ads1_ch0_V);

  const charts = [chartHR, chartEnv, chartPres, chartGSR];
  for (let i = 0; i < charts.length; i += 1) {
    if (shouldUpdateChart(i)) {
      charts[i].update("none");
    }
  }
  chartUpdateCycle += 1;
}

function applyRealtimeEvent(payload) {
  const d = payload.data;
  const warm = payload.warm;

  updateChartsWithGuardrail(d);
  updateCards(d);
  updateAlert(d);

  for (const card of metricCards) {
    card.classList.toggle("stale", !!d.sensor_stale);
  }

  if (diskWarn && diskText && d.disk_free_gb !== null && d.disk_free_gb !== undefined) {
    if (d.disk_free_gb < 2) {
      diskText.textContent = `Disk space low — ${d.disk_free_gb} GB remaining. Recording may fail soon.`;
      diskWarn.classList.add("visible");
    } else {
      diskWarn.classList.remove("visible");
    }
  }

  if (fpsBadge) {
    fpsBadge.textContent =
      d.camera_fps !== null && d.camera_fps !== undefined
        ? d.camera_fps.toFixed(1) + " fps"
        : "— fps";
  }

  const now = new Date().toLocaleTimeString();
  if (warm && warm.warmupActive) {
    if (lastUpdate) {
      lastUpdate.textContent = `Last update: ${now} - Warmup ${warm.countdown}s`;
    }
  } else if (lastUpdate) {
    lastUpdate.textContent = `Last update: ${now}`;
  }
  if (footerTs) {
    footerTs.textContent = d.timestamp_utc || now;
  }
  syncStatusDot(warm);
}

function flushRealtimeFrame() {
  realtimeFlushRafId = null;
  if (!pendingRealtimeEvent) return;

  const frameStart = performance.now();
  const payload = pendingRealtimeEvent;
  pendingRealtimeEvent = null;
  applyRealtimeEvent(payload);
  updateFrameGuardrail(performance.now() - frameStart);

  if (pendingRealtimeEvent) {
    realtimeFlushRafId = window.requestAnimationFrame(flushRealtimeFrame);
  }
}

function scheduleRealtimeEvent(data, warm) {
  pendingRealtimeEvent = { data, warm };
  if (realtimeFlushRafId === null) {
    realtimeFlushRafId = window.requestAnimationFrame(flushRealtimeFrame);
  }
}

function syncStatusDot(warm) {
  if (!statusDot) return;
  if (!warm) {
    statusDot.className = "live";
    return;
  }

  const state = String(warm.runtimeState || "").toUpperCase();
  if (state === "RUNNING") {
    statusDot.className = "live";
    return;
  }
  if (state === "WARMUP" || state === "INIT") {
    statusDot.className = "warmup";
    return;
  }
  statusDot.className = "error";
}

function connect() {
  if (!sseClient) return;
  if (dashboardStream) dashboardStream.close();
  dashboardStream = sseClient.connect({
    url: "/stream",
    reconnectMs: RECONNECT_MS,
    reconnectMaxMs: 20000,
    pauseWhenHidden: true,
    parseJson: true,
    onOpen: () => {
      if (statusDot) statusDot.className = "live";
      console.info("[NEUROSENSE] SSE connected.");
    },
    onJson: (d) => {
      const warm = warmupUI
        ? warmupUI.apply(d, { showOverlay: true, overlayMinSeconds: 0.2 })
        : null;
      scheduleRealtimeEvent(d, warm);
    },
    onError: () => {
      if (statusDot) statusDot.className = "error";
      if (lastUpdate) lastUpdate.textContent = "Connection lost — reconnecting…";
    },
  });
}

connect();
if (chartUtils) {
  unregisterChartResize = chartUtils.registerResponsiveCharts([
    chartHR,
    chartEnv,
    chartPres,
    chartGSR,
  ]);
}

window.addEventListener("beforeunload", () => {
  if (dashboardStream) dashboardStream.close();
  if (realtimeFlushRafId !== null) {
    window.cancelAnimationFrame(realtimeFlushRafId);
    realtimeFlushRafId = null;
  }
  if (unregisterChartResize) unregisterChartResize();
});

// ── Camera status ─────────────────────────────────────────────────────────
const camStatus = byId("camera-status");
const camSection = byId("camera-section");
const cameraFeed = byId("camera-feed");
const CAMERA_RETRY_MS = 2500;
let cameraRetryTimer = null;

function scheduleCameraRetry() {
  if (cameraRetryTimer !== null) return;
  cameraRetryTimer = window.setTimeout(() => {
    cameraRetryTimer = null;
    const feed = byId("camera-feed");
    if (!feed) return;
    feed.style.display = "";
    feed.src = `/camera/stream?t=${Date.now()}`;
  }, CAMERA_RETRY_MS);
}

function onCameraLoad() {
  if (cameraRetryTimer !== null) {
    window.clearTimeout(cameraRetryTimer);
    cameraRetryTimer = null;
  }
  if (camStatus) {
    camStatus.textContent = "Live";
    camStatus.className = "";
  }
  if (camSection) camSection.style.display = "";
}

function onCameraError() {
  if (camStatus) {
    camStatus.textContent = "Camera unavailable - retrying...";
    camStatus.className = "error";
  }
  // Temporarily hide frame while stream is reconnecting.
  const feed = byId("camera-feed");
  if (feed) feed.style.display = "none";
  scheduleCameraRetry();
}

function bindCameraFeedEvents() {
  if (!cameraFeed) return;
  cameraFeed.addEventListener("load", onCameraLoad);
  cameraFeed.addEventListener("error", onCameraError);
}

bindCameraFeedEvents();

// ── GSR Recalibration ─────────────────────────────────────────────────────
async function recalibrateGSR() {
  const btn = byId("btn-recal");
  const status = byId("recal-status");
  if (!btn || !status) return;

  btn.disabled = true;
  status.className = "busy";

  // Countdown: ask user to release sensor
  const messages = [
    "Lepas sensor… 3",
    "Lepas sensor… 2",
    "Lepas sensor… 1",
    "Mengambil baseline…",
  ];
  for (const msg of messages) {
    status.textContent = msg;
    await new Promise((r) => setTimeout(r, 1000));
  }

  try {
    const res = await fetch("/recalibrate/gsr", { method: "POST" });
    const data = await res.json();
    if (data.status === "ok") {
      const b10 = data.baseline_10bit ?? "?";
      const mx = data.max_conductance_us ?? "?";
      status.className = "ok";
      status.textContent = `✓ Baseline ${b10} (max ≈ ${mx} µS)`;
    } else {
      status.className = "err";
      status.textContent = "✗ " + (data.message || "error");
    }
  } catch (e) {
    status.className = "err";
    status.textContent = "✗ Koneksi gagal";
  }

  btn.disabled = false;
  // Clear status after 6 s
  setTimeout(() => {
    status.textContent = "";
    status.className = "";
  }, 6000);
}
