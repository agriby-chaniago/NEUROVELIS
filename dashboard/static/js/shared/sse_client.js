"use strict";

(function (global) {
  function connect(options) {
    const opts = options || {};
    const url = String(opts.url || "");
    const reconnectBaseMs = Number.isFinite(opts.reconnectMs)
      ? Math.max(250, opts.reconnectMs)
      : 3000;
    const reconnectMaxMs = Number.isFinite(opts.reconnectMaxMs)
      ? Math.max(reconnectBaseMs, opts.reconnectMaxMs)
      : 20000;
    const parseJson = opts.parseJson === true;
    const pauseWhenHidden = opts.pauseWhenHidden === true;

    if (!url) {
      throw new Error("NeurosenseSSEClient.connect requires a non-empty url");
    }

    let es = null;
    let closed = false;
    let retryTimer = null;
    let reconnectAttempt = 0;

    function clearRetryTimer() {
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
    }

    function computeReconnectDelayMs() {
      const expDelay = reconnectBaseMs * Math.pow(2, Math.max(0, reconnectAttempt - 1));
      const clamped = Math.min(reconnectMaxMs, expDelay);
      const jitter = Math.floor(Math.random() * 250);
      return clamped + jitter;
    }

    function scheduleReconnect() {
      if (closed) return;
      clearRetryTimer();
      reconnectAttempt += 1;
      const delayMs = computeReconnectDelayMs();
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        start();
      }, delayMs);
    }

    function start() {
      if (closed) return;
      clearRetryTimer();

      if (pauseWhenHidden && document.visibilityState === "hidden") {
        scheduleReconnect();
        return;
      }

      es = new EventSource(url);

      es.onopen = (event) => {
        reconnectAttempt = 0;
        if (typeof opts.onOpen === "function") opts.onOpen(event);
      };

      es.onmessage = (event) => {
        if (parseJson && typeof opts.onJson === "function") {
          try {
            opts.onJson(JSON.parse(event.data), event);
          } catch (_err) {
            if (typeof opts.onParseError === "function") {
              opts.onParseError(event.data, event);
            }
          }
        }
        if (typeof opts.onMessage === "function") {
          opts.onMessage(event.data, event);
        }
      };

      es.onerror = (event) => {
        if (typeof opts.onError === "function") opts.onError(event);
        if (es) {
          es.close();
          es = null;
        }
        scheduleReconnect();
      };
    }

    function onVisibilityChange() {
      if (!pauseWhenHidden || closed) return;

      if (document.visibilityState === "hidden") {
        clearRetryTimer();
        if (es) {
          es.close();
          es = null;
        }
        return;
      }

      if (!es) {
        reconnectAttempt = 0;
        start();
      }
    }

    start();
    if (pauseWhenHidden) {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return {
      close() {
        closed = true;
        clearRetryTimer();
        if (pauseWhenHidden) {
          document.removeEventListener("visibilitychange", onVisibilityChange);
        }
        if (es) {
          es.close();
          es = null;
        }
      },
    };
  }

  global.NeurosenseSSEClient = {
    connect,
  };
})(window);
