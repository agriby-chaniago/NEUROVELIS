"use strict";

(function (global) {
  function toNumber(val) {
    const n = Number(val);
    return Number.isFinite(n) ? n : null;
  }

  function parseWarmupFromReason(reason) {
    const txt = String(reason || "");
    const m = txt.match(/CLASS_WARMUP\s*:\s*([0-9]+(?:\.[0-9]+)?)s/i);
    if (!m) return null;
    const n = Number(m[1]);
    return Number.isFinite(n) ? n : null;
  }

  function normalizeState(data) {
    const reason = String(data.model_runtime_reason || data.model_alert_reasons || "");
    const label = String(data.model_label_top1 || "").toLowerCase();

    let remaining = toNumber(data.model_warmup_remaining_s);
    if (remaining === null) remaining = parseWarmupFromReason(reason);
    if (remaining === null) remaining = 0;
    if (remaining < 0) remaining = 0;

    const warmupActive = Boolean(data.model_warmup_active) || remaining > 0;
    let runtimeState = String(data.model_runtime_state || "").toUpperCase();

    if (!runtimeState) {
      if (warmupActive) runtimeState = "WARMUP";
      else if (/SENSOR_NOT_TOUCHED/i.test(reason)) runtimeState = "WAITING_SENSOR";
      else if (label && label !== "unknown") runtimeState = "RUNNING";
      else runtimeState = "DEGRADED";
    }

    const countdown = Math.max(0, Math.ceil(remaining));
    return {
      warmupActive,
      remaining,
      countdown,
      runtimeState,
      runtimeReason: reason || "-",
    };
  }

  function stateText(state) {
    switch (state) {
      case "RUNNING":
        return "LIVE";
      case "WARMUP":
        return "WARMUP";
      case "WAITING_SENSOR":
        return "WAIT SENSOR";
      case "INIT":
        return "INIT";
      default:
        return "DEGRADED";
    }
  }

  function stateClass(state) {
    const s = String(state || "").toLowerCase();
    if (s === "running") return "is-running";
    if (s === "warmup") return "is-warmup";
    if (s === "waiting_sensor") return "is-waiting";
    if (s === "init") return "is-init";
    return "is-degraded";
  }

  function apply(data, options) {
    const opts = options || {};
    const warm = normalizeState(data || {});

    const banner = document.getElementById("warmup-banner");
    const bannerState = document.getElementById("warmup-banner-state");
    const bannerCountdown = document.getElementById("warmup-banner-countdown");
    const bannerReason = document.getElementById("warmup-banner-reason");

    const shouldShowBanner = warm.warmupActive || warm.runtimeState === "WAITING_SENSOR";
    if (banner) {
      banner.classList.toggle("visible", shouldShowBanner);
      banner.classList.remove("is-running", "is-warmup", "is-waiting", "is-init", "is-degraded");
      banner.classList.add(stateClass(warm.runtimeState));
    }
    if (bannerState) bannerState.textContent = stateText(warm.runtimeState);
    if (bannerCountdown) {
      bannerCountdown.textContent = warm.warmupActive ? `${warm.countdown}s` : "--";
    }
    if (bannerReason) {
      if (warm.runtimeState === "WAITING_SENSOR") {
        bannerReason.textContent = "Pastikan sensor disentuh agar warmup dimulai.";
      } else if (warm.warmupActive) {
        bannerReason.textContent = "Stabilisasi data model sedang berlangsung.";
      } else {
        bannerReason.textContent = warm.runtimeReason;
      }
    }

    const pill = document.getElementById(opts.pillId || "runtime-state-pill");
    if (pill) {
      pill.textContent = stateText(warm.runtimeState);
      pill.classList.remove("is-running", "is-warmup", "is-waiting", "is-init", "is-degraded");
      pill.classList.add(stateClass(warm.runtimeState));
    }

    const showOverlay = opts.showOverlay !== false;
    const overlayMinSeconds = Number.isFinite(opts.overlayMinSeconds)
      ? opts.overlayMinSeconds
      : 0.2;

    const overlay = document.getElementById("warmup-overlay");
    const overlayNumber = document.getElementById("warmup-overlay-number");
    const overlayLabel = document.getElementById("warmup-overlay-label");

    const shouldShowOverlay =
      showOverlay && warm.warmupActive && warm.remaining >= overlayMinSeconds;

    if (overlay) {
      overlay.classList.toggle("show", shouldShowOverlay);
    }
    if (overlayNumber) {
      overlayNumber.textContent = String(Math.max(1, warm.countdown));
    }
    if (overlayLabel) {
      overlayLabel.textContent = "Model warmup in progress";
    }

    return warm;
  }

  global.NeurosenseWarmupUI = {
    normalizeState,
    apply,
  };
})(window);
