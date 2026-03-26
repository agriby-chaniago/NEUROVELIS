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
const cameraFeed = document.getElementById("camera-feed");
const meshCanvas = document.getElementById("face-mesh-overlay");
const meshCtx = meshCanvas ? meshCanvas.getContext("2d") : null;

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
const OUTER_LIPS = [
  61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317,
  14, 87, 178, 88, 95, 78, 61,
];
const INNER_LIPS = [
  78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87,
  178, 88, 95, 78,
];
const LEFT_BROW = [70, 63, 105, 66, 107, 55, 65, 52, 53, 46];
const RIGHT_BROW = [336, 296, 334, 293, 300, 285, 295, 282, 283, 276];
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
  drawContourPath(projected, OUTER_LIPS);
  drawContourPath(projected, INNER_LIPS);
  drawContourPath(projected, LEFT_BROW);
  drawContourPath(projected, RIGHT_BROW);
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
  setText("val-reason", data.model_alert_reasons || "-");
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

function connectMeshStream() {
  const es = new EventSource("/model/mesh_stream");

  es.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_err) {
      return;
    }
    drawFaceMesh(data.model_face_landmarks);
  };

  es.onerror = () => {
    es.close();
    clearMeshOverlay();
    setTimeout(connectMeshStream, RECONNECT_MS);
  };
}

loadMeshTopology().then(connectMeshStream);

const camStatus = document.getElementById("camera-status");
const camSection = document.getElementById("camera-section");

function onCameraLoad() {
  if (camStatus) {
    camStatus.textContent = "Live";
    camStatus.className = "";
  }
  syncMeshCanvasSize();
}

function onCameraError() {
  if (camStatus) {
    camStatus.textContent = "Camera not available";
    camStatus.className = "error";
  }
  clearMeshOverlay();
  const feed = document.getElementById("camera-feed");
  if (feed) feed.style.display = "none";
  if (camSection) camSection.style.display = "none";
}

window.addEventListener("resize", syncMeshCanvasSize);
