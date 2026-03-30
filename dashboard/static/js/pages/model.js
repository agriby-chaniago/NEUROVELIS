"use strict";

const MAX_POINTS = 60;
const RECONNECT_MS = 3000;
const CLASS_ORDER = ["anxiety", "stress", "depression"];

Chart.defaults.color = "#52697e";
Chart.defaults.borderColor = "#d0d9e4";
Chart.defaults.font.family = '"IBM Plex Sans", "Segoe UI", Roboto, sans-serif';
Chart.defaults.font.size = 11;
const warmupUI = window.NeurosenseWarmupUI || null;

const labels = [];
const anxietyTrendBuf = [];
const stressTrendBuf = [];
const depressionTrendBuf = [];

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
    labels: ["Anxiety", "Stress", "Depression"],
    datasets: [
      {
        label: "Chance",
        data: [0, 0, 0],
        backgroundColor: ["#1a5fad", "#c25d00", "#b71c1c"],
        borderColor: ["#1a5fad", "#c25d00", "#b71c1c"],
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
        label: "Anxiety",
        data: anxietyTrendBuf,
        borderColor: "#1a5fad",
        backgroundColor: "#1a5fad22",
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: false,
      },
      {
        label: "Stress",
        data: stressTrendBuf,
        borderColor: "#c25d00",
        backgroundColor: "#c25d0022",
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: false,
      },
      {
        label: "Depression",
        data: depressionTrendBuf,
        borderColor: "#b71c1c",
        backgroundColor: "#b71c1c22",
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
    plugins: { legend: { display: true, position: "bottom" } },
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
const cameraFeed = document.getElementById("camera-feed");
const meshCanvas = document.getElementById("face-mesh-overlay");
const meshCtx = meshCanvas ? meshCanvas.getContext("2d") : null;
const sseClient = window.NeurosenseSSEClient || null;
const chartUtils = window.NeurosenseChartUtils || null;
let modelStream = null;
let meshStream = null;
let unregisterChartResize = null;

const MESH_STYLE = {
  smoothingAlpha: 0.72,
  boxColor: "255, 48, 48",
  boxAlpha: 0.95,
  boxWidth: 2.2,
  contourColor: "87, 255, 87",
  contourAlpha: 0.96,
  contourWidth: 1.4,
  pointColor: "255, 48, 48",
  pointAlpha: 0.88,
  pointRadius: 1.35,
};

const FACE_OVAL = [
  10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
  400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21,
  54, 103, 67, 109, 10,
];
const LEFT_EYE_RING = [
  33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7,
  33,
];
const RIGHT_EYE_RING = [
  263, 466, 388, 387, 386, 385, 384, 398, 362, 382, 381, 380, 374, 373, 390,
  249, 263,
];
const UPPER_LIP_OUTER = [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291];
const UPPER_LIP_INNER = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308];
const LOWER_LIP_OUTER = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291];
const LOWER_LIP_INNER = [78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308];

const LEFT_BROW_UPPER = [70, 63, 105, 66, 107];
const LEFT_BROW_LOWER = [46, 53, 52, 65, 55];
const RIGHT_BROW_UPPER = [336, 296, 334, 293, 300];
const RIGHT_BROW_LOWER = [276, 283, 282, 295, 285];
const NOSE_BRIDGE = [168, 6, 197, 195, 5, 4, 1, 19, 94, 2];
const NOSE_BASE = [98, 97, 2, 326, 327];

let meshEdges = [];
let previousProjected = null;
let meshDisplayWidth = 0;
let meshDisplayHeight = 0;

function syncMeshCanvasSize() {
  if (!meshCanvas || !cameraFeed) return;
  const width = Math.floor(cameraFeed.clientWidth);
  const height = Math.floor(cameraFeed.clientHeight);
  if (width <= 0 || height <= 0) return;

  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const pixelWidth = Math.floor(width * dpr);
  const pixelHeight = Math.floor(height * dpr);

  meshDisplayWidth = width;
  meshDisplayHeight = height;

  if (meshCanvas.width !== pixelWidth || meshCanvas.height !== pixelHeight) {
    meshCanvas.width = pixelWidth;
    meshCanvas.height = pixelHeight;
    if (meshCtx) {
      // Draw in CSS pixels while preserving high-DPI sharpness.
      meshCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  }
}

function clearMeshOverlay() {
  if (!meshCanvas || !meshCtx) return;
  syncMeshCanvasSize();
  meshCtx.clearRect(0, 0, meshDisplayWidth, meshDisplayHeight);
  previousProjected = null;
}

function getCoverProjection() {
  const srcWidth = Number(cameraFeed?.naturalWidth || 0);
  const srcHeight = Number(cameraFeed?.naturalHeight || 0);

  if (
    srcWidth <= 0 ||
    srcHeight <= 0 ||
    meshDisplayWidth <= 0 ||
    meshDisplayHeight <= 0
  ) {
    return {
      offsetX: 0,
      offsetY: 0,
      drawWidth: meshDisplayWidth,
      drawHeight: meshDisplayHeight,
    };
  }

  // Match CSS object-fit: cover geometry used by #camera-feed.
  const scale = Math.max(
    meshDisplayWidth / srcWidth,
    meshDisplayHeight / srcHeight,
  );
  const drawWidth = srcWidth * scale;
  const drawHeight = srcHeight * scale;

  return {
    offsetX: (meshDisplayWidth - drawWidth) / 2,
    offsetY: (meshDisplayHeight - drawHeight) / 2,
    drawWidth,
    drawHeight,
  };
}

function projectLandmarks(landmarks) {
  const projection = getCoverProjection();

  return landmarks.map((point) => {
    if (!Array.isArray(point) || point.length < 2) return null;
    const x = projection.offsetX + Number(point[0]) * projection.drawWidth;
    const y = projection.offsetY + Number(point[1]) * projection.drawHeight;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return [x, y];
  });
}

function smoothProjected(projected) {
  if (!Array.isArray(projected) || projected.length === 0) return projected;
  if (
    !Array.isArray(previousProjected) ||
    previousProjected.length !== projected.length
  ) {
    previousProjected = projected;
    return projected;
  }

  const alpha = MESH_STYLE.smoothingAlpha;
  const smoothed = projected.map((curr, idx) => {
    const prev = previousProjected[idx];
    if (!curr || !prev) return curr;
    const x = prev[0] + (curr[0] - prev[0]) * alpha;
    const y = prev[1] + (curr[1] - prev[1]) * alpha;
    return [x, y];
  });
  previousProjected = smoothed;
  return smoothed;
}

function drawContourPath(projected, indices) {
  if (!meshCtx || !Array.isArray(indices) || indices.length < 2) return;
  let hasAnyPoint = false;
  meshCtx.beginPath();
  for (let i = 0; i < indices.length; i += 1) {
    const p = projected[indices[i]];
    if (!p) continue;
    if (!hasAnyPoint) {
      meshCtx.moveTo(p[0], p[1]);
      hasAnyPoint = true;
    } else {
      meshCtx.lineTo(p[0], p[1]);
    }
  }
  if (hasAnyPoint) {
    meshCtx.stroke();
  }
}

function drawRotatedBoundingBox(projected) {
  if (!meshCtx) return;
  const left = projected[234];
  const right = projected[454];
  const forehead = projected[10];
  const chin = projected[152];
  if (!left || !right || !forehead || !chin) return;

  const cx = (left[0] + right[0] + forehead[0] + chin[0]) / 4;
  const cy = (left[1] + right[1] + forehead[1] + chin[1]) / 4;

  const faceWidth = Math.hypot(right[0] - left[0], right[1] - left[1]);
  const faceHeight = Math.hypot(chin[0] - forehead[0], chin[1] - forehead[1]);
  if (!Number.isFinite(faceWidth) || !Number.isFinite(faceHeight)) return;

  const boxWidth = faceWidth * 1.55;
  const boxHeight = faceHeight * 1.55;
  const angle = Math.atan2(right[1] - left[1], right[0] - left[0]);
  const cosA = Math.cos(angle);
  const sinA = Math.sin(angle);
  const hw = boxWidth / 2;
  const hh = boxHeight / 2;

  const corners = [
    [-hw, -hh],
    [hw, -hh],
    [hw, hh],
    [-hw, hh],
  ].map(([x, y]) => [cx + x * cosA - y * sinA, cy + x * sinA + y * cosA]);

  meshCtx.strokeStyle = `rgba(${MESH_STYLE.boxColor}, ${MESH_STYLE.boxAlpha})`;
  meshCtx.lineWidth = MESH_STYLE.boxWidth;
  meshCtx.beginPath();
  meshCtx.moveTo(corners[0][0], corners[0][1]);
  meshCtx.lineTo(corners[1][0], corners[1][1]);
  meshCtx.lineTo(corners[2][0], corners[2][1]);
  meshCtx.lineTo(corners[3][0], corners[3][1]);
  meshCtx.closePath();
  meshCtx.stroke();
}

function drawMeshPoints(projected) {
  if (!meshCtx) return;
  meshCtx.fillStyle = `rgba(${MESH_STYLE.pointColor}, ${MESH_STYLE.pointAlpha})`;
  for (let i = 0; i < projected.length; i += 1) {
    const p = projected[i];
    if (!p) continue;
    meshCtx.beginPath();
    meshCtx.arc(p[0], p[1], MESH_STYLE.pointRadius, 0, Math.PI * 2);
    meshCtx.fill();
  }
}

function drawFaceMesh(landmarks) {
  if (!meshCanvas || !meshCtx) return;
  syncMeshCanvasSize();
  meshCtx.clearRect(0, 0, meshDisplayWidth, meshDisplayHeight);
  if (!Array.isArray(landmarks) || landmarks.length === 0) return;

  const projected = smoothProjected(projectLandmarks(landmarks));
  drawRotatedBoundingBox(projected);

  meshCtx.strokeStyle = `rgba(${MESH_STYLE.contourColor}, ${MESH_STYLE.contourAlpha})`;
  meshCtx.lineWidth = MESH_STYLE.contourWidth;
  drawContourPath(projected, FACE_OVAL);
  drawContourPath(projected, LEFT_EYE_RING);
  drawContourPath(projected, RIGHT_EYE_RING);
  drawContourPath(projected, UPPER_LIP_OUTER);
  drawContourPath(projected, UPPER_LIP_INNER);
  drawContourPath(projected, LOWER_LIP_OUTER);
  drawContourPath(projected, LOWER_LIP_INNER);
  drawContourPath(projected, LEFT_BROW_UPPER);
  drawContourPath(projected, LEFT_BROW_LOWER);
  drawContourPath(projected, RIGHT_BROW_UPPER);
  drawContourPath(projected, RIGHT_BROW_LOWER);
  drawContourPath(projected, NOSE_BRIDGE);
  drawContourPath(projected, NOSE_BASE);

  drawMeshPoints(projected);
}

async function loadMeshTopology() {
  try {
    const res = await fetch("/model/mesh_topology", { cache: "no-store" });
    const data = await res.json();
    meshEdges = Array.isArray(data.edges) ? data.edges : [];
  } catch (_err) {
    meshEdges = [];
  }
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value;
}

function fmtNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toFixed(digits);
}

function formatLabel(label) {
  if (!label) return "UNKNOWN";
  return String(label).toUpperCase();
}

function getTopChance(data) {
  const chances = {
    anxiety: Number(
      data.model_chance_anxiety ?? data.model_probs_anxiety ?? NaN,
    ),
    stress: Number(data.model_chance_stress ?? data.model_probs_stress ?? NaN),
    depression: Number(
      data.model_chance_depression ?? data.model_probs_depression ?? NaN,
    ),
  };

  const rawLabel = String(data.model_label_raw_top1 || "").toLowerCase();
  if (CLASS_ORDER.includes(rawLabel) && Number.isFinite(chances[rawLabel])) {
    return chances[rawLabel];
  }

  const label = String(data.model_label_top1 || "").toLowerCase();
  if (CLASS_ORDER.includes(label) && Number.isFinite(chances[label])) {
    return chances[label];
  }

  const values = CLASS_ORDER.map((k) => chances[k]).filter((v) =>
    Number.isFinite(v),
  );
  return values.length ? Math.max(...values) : null;
}

function updateSignalStatus(data, warm) {
  const state = String(
    warm?.runtimeState || data.model_runtime_state || "DEGRADED",
  ).toUpperCase();

  if (state === "RUNNING") {
    setText("val-signal", "OK");
    setText("val-state", "RUNNING");
    return;
  }
  if (state === "WARMUP") {
    setText("val-signal", "WARMUP");
    setText("val-state", "WARMUP");
    return;
  }
  if (state === "WAITING_SENSOR") {
    setText("val-signal", "WAIT SENSOR");
    setText("val-state", "WAITING");
    return;
  }
  if (state === "INIT") {
    setText("val-signal", "INIT");
    setText("val-state", "INIT");
    return;
  }
  setText("val-signal", "NO SIGNAL");
  setText("val-state", "DEGRADED");
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

function updateCards(data, warm) {
  const topClass = formatLabel(data.model_label_top1);
  const conf = getTopChance(data);
  const fallbackConf = data.model_confidence_top1;
  const confPct =
    conf !== null && conf !== undefined
      ? `${(Number(conf) * 100).toFixed(1)}`
      : fallbackConf !== null && fallbackConf !== undefined
        ? `${(Number(fallbackConf) * 100).toFixed(1)}`
        : "-";
  const latency =
    data.model_latency_ms !== null && data.model_latency_ms !== undefined
      ? String(data.model_latency_ms)
      : "-";

  const hr = data.hr_valid === false ? "-" : fmtNumber(data.heart_rate_bpm, 0);
  const spo2 =
    data.spo2_valid === false ? "-" : fmtNumber(data.spo2_percent, 1);
  const gsr = fmtNumber(data.gsr_conductance_us, 4);

  setText("val-top-class", topClass);
  setText("val-confidence", confPct);
  setText("val-hr", hr);
  setText("val-spo2", spo2);
  setText("val-gsr", gsr);
  setText("val-latency", latency);

  const warmCountdown = warm?.warmupActive ? `${warm.countdown}s` : null;
  setText(
    "val-reason",
    warmCountdown ||
      data.model_runtime_reason ||
      data.model_alert_reasons ||
      "-",
  );
}

function updateCharts(data) {
  const probs = [
    Number(data.model_chance_anxiety ?? data.model_probs_anxiety ?? 0),
    Number(data.model_chance_stress ?? data.model_probs_stress ?? 0),
    Number(data.model_chance_depression ?? data.model_probs_depression ?? 0),
  ];
  probChart.data.datasets[0].data = probs;
  probChart.update();

  pushLabel(data.model_timestamp_utc || data.timestamp_utc);
  pushVal(anxietyTrendBuf, probs[0]);
  pushVal(stressTrendBuf, probs[1]);
  pushVal(depressionTrendBuf, probs[2]);
  confChart.update();
}

function connect() {
  if (!sseClient) return;
  if (modelStream) modelStream.close();

  modelStream = sseClient.connect({
    url: "/stream",
    reconnectMs: RECONNECT_MS,
    reconnectMaxMs: 20000,
    pauseWhenHidden: true,
    parseJson: true,
    onOpen: () => {
      statusDot.className = "live";
      lastUpdate.textContent = "Model stream connected";
    },
    onJson: (data) => {
      const warm = warmupUI
        ? warmupUI.apply(data, { showOverlay: true, overlayMinSeconds: 0.2 })
        : null;

      updateCards(data, warm);
      updateSignalStatus(data, warm);
      updateAlert(data);
      updateCharts(data);

      if (data.camera_fps !== null && data.camera_fps !== undefined) {
        setText("camera-fps", `${Number(data.camera_fps).toFixed(1)} fps`);
      }

      const now = new Date().toLocaleTimeString();
      if (warm && warm.warmupActive) {
        statusDot.className = "warmup";
        lastUpdate.textContent = `Warmup in progress - ${warm.countdown}s`;
      } else if (warm && warm.runtimeState === "RUNNING") {
        statusDot.className = "live";
        lastUpdate.textContent = `Last update: ${now}`;
      } else {
        statusDot.className = "error";
        lastUpdate.textContent = "Waiting for stable signal...";
      }
      footerTs.textContent =
        data.model_timestamp_utc || data.timestamp_utc || now;
    },
    onError: () => {
      statusDot.className = "error";
      lastUpdate.textContent = "Connection lost — reconnecting...";
    },
  });
}

connect();

function connectMeshStream() {
  if (!sseClient) return;
  if (meshStream) meshStream.close();

  meshStream = sseClient.connect({
    url: "/model/mesh_stream",
    reconnectMs: RECONNECT_MS,
    reconnectMaxMs: 12000,
    pauseWhenHidden: true,
    parseJson: true,
    onJson: (data) => {
      drawFaceMesh(data.model_face_landmarks);
    },
    onError: () => {
      clearMeshOverlay();
    },
  });
}

loadMeshTopology().then(connectMeshStream);
if (chartUtils) {
  unregisterChartResize = chartUtils.registerResponsiveCharts([
    probChart,
    confChart,
  ]);
}

const camStatus = document.getElementById("camera-status");
const camSection = document.getElementById("camera-section");
const modelCameraFeed = document.getElementById("camera-feed");
const CAMERA_RETRY_MS = 2500;
let cameraRetryTimer = null;

function scheduleCameraRetry() {
  if (cameraRetryTimer !== null) return;
  cameraRetryTimer = window.setTimeout(() => {
    cameraRetryTimer = null;
    const feed = document.getElementById("camera-feed");
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
  syncMeshCanvasSize();
}

function onCameraError() {
  if (camStatus) {
    camStatus.textContent = "Camera unavailable - retrying...";
    camStatus.className = "error";
  }
  clearMeshOverlay();
  const feed = document.getElementById("camera-feed");
  if (feed) feed.style.display = "none";
  scheduleCameraRetry();
}

function bindCameraFeedEvents() {
  if (!modelCameraFeed) return;
  modelCameraFeed.addEventListener("load", onCameraLoad);
  modelCameraFeed.addEventListener("error", onCameraError);
}

bindCameraFeedEvents();

window.addEventListener("resize", syncMeshCanvasSize);

window.addEventListener("beforeunload", () => {
  if (modelStream) modelStream.close();
  if (meshStream) meshStream.close();
  if (unregisterChartResize) unregisterChartResize();
});
