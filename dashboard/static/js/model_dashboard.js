"use strict";

const MAX_POINTS = 60;
const RECONNECT_MS = 3000;
const CLASS_ORDER = ["normal", "anxiety", "stress", "depression"];

Chart.defaults.color = "#52697e";
Chart.defaults.borderColor = "#d0d9e4";
Chart.defaults.font.family =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
Chart.defaults.font.size = 11;

const labels = [];
const confidenceBuf = [];

function pushLabel(ts) {
  const d = ts ? new Date(ts) : new Date();
  labels.push(
    d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }),
  );
  if (labels.length > MAX_POINTS) labels.shift();
}

function pushVal(buf, val) {
  buf.push(val !== null && val !== undefined ? val : NaN);
  if (buf.length > MAX_POINTS) buf.shift();
}

const probChart = new Chart(document.getElementById("chart-prob"), {
  type: "bar",
  data: {
    labels: ["Normal", "Anxiety", "Stress", "Depression"],
    datasets: [
      {
        label: "Probability",
        data: [0, 0, 0, 0],
        backgroundColor: ["#1e7845", "#1a5fad", "#c25d00", "#b71c1c"],
        borderColor: ["#1e7845", "#1a5fad", "#c25d00", "#b71c1c"],
        borderWidth: 1,
      },
    ],
  },
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      y: {
        min: 0,
        max: 1,
        ticks: {
          callback: (v) => `${Math.round(v * 100)}%`,
          font: { size: 9 },
        },
      },
      x: { ticks: { font: { size: 9 } } },
    },
  },
});

const confChart = new Chart(document.getElementById("chart-conf"), {
  type: "line",
  data: {
    labels,
    datasets: [
      {
        label: "Top-1 Confidence",
        data: confidenceBuf,
        borderColor: "#1e7845",
        backgroundColor: "#1e784522",
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: false,
      },
    ],
  },
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      y: {
        min: 0,
        max: 1,
        ticks: {
          callback: (v) => `${Math.round(v * 100)}%`,
          font: { size: 9 },
        },
      },
      x: { ticks: { maxTicksLimit: 6, font: { size: 9 } } },
    },
  },
});

const statusDot = document.getElementById("status-dot");
const lastUpdate = document.getElementById("last-update");
const footerTs = document.getElementById("footer-ts");

function setText(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value;
}

function formatLabel(label) {
  if (!label) return "UNKNOWN";
  return String(label).toUpperCase();
}

function updateSignalStatus(data) {
  const hasClass =
    !!data.model_label_top1 && data.model_label_top1 !== "unknown";
  setText("val-signal", hasClass ? "OK" : "NO SIGNAL");
  setText("val-state", hasClass ? "RUNNING" : "DEGRADED");
}

function updateAlert(data) {
  const banner = document.getElementById("model-alert");
  const text = document.getElementById("model-alert-text");
  const active = !!data.model_alert_active;
  if (active) {
    const msg = data.model_alert_reasons || "MODEL ALERT";
    text.textContent = `ALERT: ${msg}`;
    banner.classList.add("visible");
  } else {
    banner.classList.remove("visible");
  }
}

function updateCards(data) {
  const topClass = formatLabel(data.model_label_top1);
  const conf = data.model_confidence_top1;
  const confPct =
    conf !== null && conf !== undefined
      ? `${(Number(conf) * 100).toFixed(1)}`
      : "—";
  const latency =
    data.model_latency_ms !== null && data.model_latency_ms !== undefined
      ? String(data.model_latency_ms)
      : "—";

  setText("val-top-class", topClass);
  setText("val-confidence", confPct);
  setText("val-latency", latency);
  setText("val-reason", data.model_alert_reasons || "—");
}

function updateCharts(data) {
  const probs = [
    Number(data.model_probs_normal || 0),
    Number(data.model_probs_anxiety || 0),
    Number(data.model_probs_stress || 0),
    Number(data.model_probs_depression || 0),
  ];
  probChart.data.datasets[0].data = probs;
  probChart.update();

  pushLabel(data.model_timestamp_utc || data.timestamp_utc);
  const conf = data.model_confidence_top1;
  pushVal(
    confidenceBuf,
    conf !== null && conf !== undefined ? Number(conf) : null,
  );
  confChart.update();
}

function connect() {
  const es = new EventSource("/stream");

  es.onopen = () => {
    statusDot.className = "live";
    lastUpdate.textContent = "Model stream connected";
  };

  es.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_err) {
      return;
    }

    updateCards(data);
    updateSignalStatus(data);
    updateAlert(data);
    updateCharts(data);

    if (data.camera_fps !== null && data.camera_fps !== undefined) {
      setText("camera-fps", `${Number(data.camera_fps).toFixed(1)} fps`);
    }

    const now = new Date().toLocaleTimeString();
    lastUpdate.textContent = `Last update: ${now}`;
    footerTs.textContent =
      data.model_timestamp_utc || data.timestamp_utc || now;
  };

  es.onerror = () => {
    statusDot.className = "error";
    lastUpdate.textContent = "Connection lost — reconnecting...";
    es.close();
    setTimeout(connect, RECONNECT_MS);
  };
}

connect();
