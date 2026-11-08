(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const dom = { session: byId("session-select"), export: byId("archive-export") };

  function setSession(sessionId) {
    if (!sessionId) {
      dom.export.removeAttribute("href");
      dom.export.removeAttribute("download");
      dom.export.setAttribute("aria-disabled", "true");
      return;
    }
    dom.export.href = `/api/v1/sessions/${encodeURIComponent(sessionId)}/archive/export.ndjson`;
    const safeName = sessionId.replace(/[^A-Za-z0-9._-]+/g, "_");
    dom.export.download = `archive-${safeName}.ndjson`;
    dom.export.setAttribute("aria-disabled", "false");
  }

  dom.session.addEventListener("change", () => setSession(dom.session.value));
  dom.export.addEventListener("click", (event) => {
    if (dom.export.getAttribute("aria-disabled") === "true") event.preventDefault();
  });
  const observer = new MutationObserver(() => setSession(dom.session.value));
  observer.observe(dom.session, { childList: true, subtree: true });
  setSession(dom.session.value);
})();
