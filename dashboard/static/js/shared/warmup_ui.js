"use strict";

(function (global) {
  const lastRender = {
    bannerVisible: null,
    bannerClass: "",
    bannerStateText: "",
    bannerCountdownText: "",
    bannerReasonText: "",
    pillText: "",
    pillClass: "",
    overlayVisible: null,
    overlayNumberText: "",
    overlayLabelText: "",
  };

  const warmupTicker = {
    intervalId: null,
    endsAtMs: 0,
    overlayEnabled: true,
    overlayMinSeconds: 0.2,
  };

  function setTextIfChanged(el, nextText, key) {
    if (!el) return;
    const value = String(nextText);
    if (lastRender[key] === value) return;
    el.textContent = value;
    lastRender[key] = value;
  }

  function setVisibleIfChanged(el, visible, key) {
    if (!el) return;
    const value = Boolean(visible);
    if (lastRender[key] === value) return;
    el.classList.toggle("visible", value);
    lastRender[key] = value;
  }

  function setShowIfChanged(el, visible, key) {
    if (!el) return;
    const value = Boolean(visible);
    if (lastRender[key] === value) return;
    el.classList.toggle("show", value);
    lastRender[key] = value;
  }

  function setStateClassIfChanged(el, nextClass, key) {
    if (!el) return;
    if (lastRender[key] === nextClass) return;
    el.classList.remove(
      "is-running",
      "is-warmup",
      "is-waiting",
      "is-init",
      "is-degraded",
    );
    el.classList.add(nextClass);
    lastRender[key] = nextClass;
  }

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
    const reason = String(
      data.model_runtime_reason || data.model_alert_reasons || "",
    );
    const label = String(data.model_label_top1 || "").toLowerCase();

    let remaining = toNumber(data.model_warmup_remaining_s);
    if (remaining === null) remaining = parseWarmupFromReason(reason);
    if (remaining === null) remaining = 0;
    if (remaining < 0) remaining = 0;

    const warmupActive = Boolean(data.model_warmup_active) || remaining > 0;
    let runtimeState = String(data.model_runtime_state || "").toUpperCase();

    if (!runtimeState) {
      if (warmupActive) runtimeState = "WARMUP";
      else if (/SENSOR_NOT_TOUCHED/i.test(reason))
        runtimeState = "WAITING_SENSOR";
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

  function formatCountdown(seconds) {
    const sec = Math.max(0, Number(seconds) || 0);
    if (sec < 10) {
      return `${sec.toFixed(1)}s`;
    }
    if (sec < 60) {
      return `${Math.ceil(sec)}s`;
    }
    const mins = Math.floor(sec / 60);
    const rem = Math.ceil(sec % 60);
    return `${mins}:${String(rem).padStart(2, "0")}`;
  }

  function stopWarmupTicker() {
    if (warmupTicker.intervalId !== null) {
      window.clearInterval(warmupTicker.intervalId);
      warmupTicker.intervalId = null;
    }
  }

  function refreshWarmupTicker() {
    const remaining = Math.max(0, (warmupTicker.endsAtMs - Date.now()) / 1000);
    const bannerCountdown = document.getElementById("warmup-banner-countdown");
    const overlay = document.getElementById("warmup-overlay");
    const overlayNumber = document.getElementById("warmup-overlay-number");

    setTextIfChanged(
      bannerCountdown,
      formatCountdown(remaining),
      "bannerCountdownText",
    );

    const shouldShowOverlay =
      warmupTicker.overlayEnabled &&
      remaining >= warmupTicker.overlayMinSeconds;
    setShowIfChanged(overlay, shouldShowOverlay, "overlayVisible");
    setTextIfChanged(
      overlayNumber,
      String(Math.max(1, Math.ceil(remaining))),
      "overlayNumberText",
    );

    if (remaining <= 0) {
      stopWarmupTicker();
    }
  }

  function syncWarmupTicker(warm, showOverlay, overlayMinSeconds) {
    if (!warm.warmupActive) {
      stopWarmupTicker();
      return;
    }

    warmupTicker.endsAtMs = Date.now() + Math.max(0, warm.remaining) * 1000;
    warmupTicker.overlayEnabled = showOverlay;
    warmupTicker.overlayMinSeconds = Math.max(0, overlayMinSeconds);
    refreshWarmupTicker();

    if (warmupTicker.intervalId === null) {
      warmupTicker.intervalId = window.setInterval(refreshWarmupTicker, 100);
    }
  }

  function apply(data, options) {
    const opts = options || {};
    const warm = normalizeState(data || {});

    const banner = document.getElementById("warmup-banner");
    const bannerState = document.getElementById("warmup-banner-state");
    const bannerCountdown = document.getElementById("warmup-banner-countdown");
    const bannerReason = document.getElementById("warmup-banner-reason");

    const shouldShowBanner =
      warm.warmupActive || warm.runtimeState === "WAITING_SENSOR";
    setVisibleIfChanged(banner, shouldShowBanner, "bannerVisible");
    setStateClassIfChanged(
      banner,
      stateClass(warm.runtimeState),
      "bannerClass",
    );
    setTextIfChanged(
      bannerState,
      stateText(warm.runtimeState),
      "bannerStateText",
    );
    setTextIfChanged(
      bannerCountdown,
      warm.warmupActive ? formatCountdown(warm.remaining) : "--",
      "bannerCountdownText",
    );
    if (bannerReason) {
      if (warm.runtimeState === "WAITING_SENSOR") {
        setTextIfChanged(
          bannerReason,
          "Pastikan sensor disentuh agar warmup dimulai.",
          "bannerReasonText",
        );
      } else if (warm.warmupActive) {
        setTextIfChanged(
          bannerReason,
          "Stabilisasi data model sedang berlangsung.",
          "bannerReasonText",
        );
      } else {
        setTextIfChanged(bannerReason, warm.runtimeReason, "bannerReasonText");
      }
    }

    const pill = document.getElementById(opts.pillId || "runtime-state-pill");
    if (pill) {
      setTextIfChanged(pill, stateText(warm.runtimeState), "pillText");
      setStateClassIfChanged(pill, stateClass(warm.runtimeState), "pillClass");
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

    setShowIfChanged(overlay, shouldShowOverlay, "overlayVisible");
    setTextIfChanged(
      overlayNumber,
      String(Math.max(1, Math.ceil(warm.remaining))),
      "overlayNumberText",
    );
    setTextIfChanged(
      overlayLabel,
      "Model warmup in progress",
      "overlayLabelText",
    );

    syncWarmupTicker(warm, showOverlay, overlayMinSeconds);

    return warm;
  }

  global.NeurosenseWarmupUI = {
    normalizeState,
    apply,
  };
})(window);
