"use strict";

(function (global) {
  function toChartList(charts) {
    if (!Array.isArray(charts)) return [];
    return charts.filter(
      (chart) => chart && typeof chart.resize === "function",
    );
  }

  function registerResponsiveCharts(charts, options) {
    const chartList = toChartList(charts);
    const opts = options || {};
    const root = opts.root || document.body;
    const debounceMs = Number.isFinite(opts.debounceMs)
      ? Math.max(30, opts.debounceMs)
      : 120;

    if (chartList.length === 0) {
      return () => {};
    }

    let timer = null;
    let rafId = null;
    let observer = null;

    function triggerResize() {
      if (document.visibilityState === "hidden") return;
      for (const chart of chartList) {
        try {
          chart.resize();
        } catch (_err) {
          // Ignore transient chart resize issues during layout shifts.
        }
      }
    }

    function scheduleResize() {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
      timer = window.setTimeout(() => {
        timer = null;
        if (rafId !== null) {
          window.cancelAnimationFrame(rafId);
        }
        rafId = window.requestAnimationFrame(() => {
          rafId = null;
          triggerResize();
        });
      }, debounceMs);
    }

    window.addEventListener("resize", scheduleResize);
    window.addEventListener("orientationchange", scheduleResize);
    document.addEventListener("visibilitychange", scheduleResize);

    if (typeof ResizeObserver === "function" && root) {
      observer = new ResizeObserver(() => {
        scheduleResize();
      });
      observer.observe(root);
    }

    scheduleResize();

    return function unregister() {
      window.removeEventListener("resize", scheduleResize);
      window.removeEventListener("orientationchange", scheduleResize);
      document.removeEventListener("visibilitychange", scheduleResize);

      if (observer) {
        observer.disconnect();
        observer = null;
      }

      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }

      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
        rafId = null;
      }
    };
  }

  global.NeurosenseChartUtils = {
    registerResponsiveCharts,
  };
})(window);
