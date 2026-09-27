(() => {
  if (window.__BBX_BRIDGE_INSTALLED__) return;
  window.__BBX_BRIDGE_INSTALLED__ = true;

  const MAX_BYTES = 2_000_000;

  function safeUrl(input) {
    try {
      if (input instanceof Request) return new URL(input.url, location.href);
      return new URL(String(input), location.href);
    } catch {
      return null;
    }
  }

  function interesting(url) {
    if (!url || url.origin !== location.origin) return false;
    const p = url.pathname.toLowerCase();
    return (
      p.includes("/learn/api/") ||
      p.includes("/ultra/") ||
      p.includes("/api/") ||
      p.includes("/courses/") ||
      p.includes("/terms/") ||
      p.includes("/contents/") ||
      p.includes("/grades/")
    );
  }

  function publish(url, body) {
    try {
      window.postMessage(
        {
          __bbx: true,
          type: "NETWORK_JSON",
          url: url.href,
          body
        },
        location.origin
      );
    } catch (_) {}
  }

  async function inspectFetch(input, response) {
    try {
      const url = safeUrl(input);
      if (!interesting(url) || !response?.ok) return;

      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      if (!contentType.includes("json")) return;

      const declared = Number(response.headers.get("content-length") || 0);
      if (declared && declared > MAX_BYTES) return;

      const text = await response.clone().text();
      if (!text || text.length > MAX_BYTES) return;

      publish(url, JSON.parse(text));
    } catch (_) {}
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function (...args) {
      const response = await originalFetch.apply(this, args);
      inspectFetch(args[0], response);
      return response;
    };
  }

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__bbxUrl = safeUrl(url);
    return originalOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    if (!this.__bbxObserved) {
      this.__bbxObserved = true;
      this.addEventListener("loadend", () => {
        try {
          const url = this.__bbxUrl;
          if (!interesting(url) || this.status < 200 || this.status >= 300) return;

          const contentType = (this.getResponseHeader("content-type") || "").toLowerCase();
          if (!contentType.includes("json")) return;

          let body;
          if (this.responseType === "json") {
            body = this.response;
          } else if (!this.responseType || this.responseType === "text") {
            const text = this.responseText;
            if (!text || text.length > MAX_BYTES) return;
            body = JSON.parse(text);
          } else {
            return;
          }

          publish(url, body);
        } catch (_) {}
      });
    }

    return originalSend.apply(this, args);
  };
})();
