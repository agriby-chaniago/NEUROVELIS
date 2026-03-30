"use strict";

const sseClient = window.NeurosenseSSEClient || null;
let warmupStream = null;

function connectWarmupStream() {
  if (!sseClient) return;
  if (!window.NeurosenseWarmupUI) return;
  if (warmupStream) warmupStream.close();

  warmupStream = sseClient.connect({
    url: "/stream",
    reconnectMs: 3000,
    reconnectMaxMs: 12000,
    pauseWhenHidden: true,
    parseJson: true,
    onJson: (data) => {
      window.NeurosenseWarmupUI.apply(data, { showOverlay: false });
    },
  });
}

function bindStartSessionCountdown() {
  const startForm = document.querySelector("form[action='/experiment/session/start']");
  if (!startForm) return;

  startForm.addEventListener("submit", function (e) {
    e.preventDefault();
    const overlay = document.getElementById("countdown-overlay");
    const numEl = document.getElementById("countdown-number");
    const labelEl = document.getElementById("countdown-label");
    overlay.classList.add("show");

    const steps = [
      { n: "3", label: "Get ready..." },
      { n: "2", label: "Stay still..." },
      { n: "1", label: "Almost..." },
      { n: "REC", label: "Recording started!" },
    ];

    function pop(el) {
      el.classList.remove("pop");
      void el.offsetWidth;
      el.classList.add("pop");
    }

    let i = 0;
    function tick() {
      if (i < steps.length) {
        numEl.textContent = steps[i].n;
        labelEl.textContent = steps[i].label;
        pop(numEl);
        i += 1;
        if (i < steps.length) setTimeout(tick, 1000);
        else setTimeout(() => startForm.submit(), 500);
      }
    }

    tick();
  });
}

function bindCameraFallback() {
  const cameraFeed = document.getElementById("camera-feed-exp");
  const camErr = document.getElementById("cam-err");
  if (!cameraFeed || !camErr) return;

  cameraFeed.addEventListener("error", () => {
    camErr.style.display = "block";
    cameraFeed.style.display = "none";
  });

  cameraFeed.addEventListener("load", () => {
    camErr.style.display = "none";
    cameraFeed.style.display = "";
  });
}

function pollActive() {
  fetch("/experiment/session/active")
    .then((r) => r.json())
    .then((s) => {
      const banner = document.getElementById("rec-banner");
      if (!banner) return;
      if (!s) {
        banner.classList.remove("show");
        return;
      }

      banner.classList.add("show");
  const cat = s.category ? ` - ${String(s.category).toUpperCase()}` : "";
      const title = document.getElementById("rec-title");
      const sub = document.getElementById("rec-sub");
      const bar = document.getElementById("rec-bar");

      if (title) title.textContent = `REC - ${s.respondent_id} (${s.session_id})${cat}`;
      if (sub) sub.textContent = `${s.elapsed_sec}s / ${s.duration_sec}s`;
      if (bar) {
        const pct = Math.min(100, Math.round((s.elapsed_sec / s.duration_sec) * 100));
        bar.style.width = `${pct}%`;
      }
    })
    .catch(() => {});
}

bindStartSessionCountdown();
bindCameraFallback();
connectWarmupStream();
setInterval(pollActive, 1000);

window.addEventListener("beforeunload", () => {
  if (warmupStream) warmupStream.close();
});
