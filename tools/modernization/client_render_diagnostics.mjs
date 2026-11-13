// Serialized by Playwright into the page before application code. No extra RAF,
// sleep, acknowledgement or renderer policy change: observe native callbacks.
export function installRenderDiagnostics() {
  const proof = {
    time_origin_ms: performance.timeOrigin,
    raf_count: 0, marker_count: 0, first_raf_ms: null, last_raf_ms: null,
    max_wait_ms: 0, max_callback_ms: 0, raf: [], markers: [],
  };
  globalThis.__oriRenderDiagnostics = proof;
  const nativeRAF = globalThis.requestAnimationFrame.bind(globalThis);
  globalThis.requestAnimationFrame = (callback) => {
    if (typeof callback !== 'function') return nativeRAF(callback);
    const queued = performance.now();
    return nativeRAF((stamp) => {
      const started = performance.now();
      try { return callback.call(globalThis, stamp); }
      finally {
        const ended = performance.now();
        const row = { started_ms: started, stamp_ms: stamp,
          wait_ms: started - queued, duration_ms: ended - started };
        proof.raf_count++;
        proof.first_raf_ms ??= started;
        proof.last_raf_ms = started;
        proof.max_wait_ms = Math.max(proof.max_wait_ms, row.wait_ms);
        proof.max_callback_ms = Math.max(proof.max_callback_ms, row.duration_ms);
        proof.raf.push(row);
        if (proof.raf.length > 128) proof.raf.shift();
      }
    });
  };
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      const canvas = record.target;
      if (canvas.tagName !== 'CANVAS') continue;
      proof.marker_count++;
      proof.markers.push({ at_ms: performance.now(), attribute: record.attributeName,
        old_value: record.oldValue, id: canvas.dataset.sweepId || '',
        frame: canvas.dataset.renderFrame || '', class_name: canvas.className,
        connected: canvas.isConnected, width: canvas.width, height: canvas.height });
      if (proof.markers.length > 64) proof.markers.shift();
    }
  });
  observer.observe(document, { subtree: true, attributes: true, attributeOldValue: true,
    attributeFilter: ['data-sweep-id', 'data-render-frame'] });
}
