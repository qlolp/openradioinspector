/* Display-only spectrum state. No transport, acquisition or measurement arithmetic. */
((root) => {
  "use strict";
  const CAPACITY = 40;
  const finite = (n) => typeof n === "number" && Number.isFinite(n);
  const clamp = (n) => Math.max(-Number.MAX_VALUE, Math.min(Number.MAX_VALUE, n));
  function fit(levels) {
    let low = Infinity, high = -Infinity;
    for (const value of levels) { if (!finite(value)) return null; low = Math.min(low, value); high = Math.max(high, value); }
    if (!finite(low)) return null;
    const size = Math.max(Math.abs(low), Math.abs(high), 1);
    let padding = Math.max((high / size - low / size) * 0.08 * size, 1);
    let bounds = [clamp(low - padding), clamp(high + padding)];
    if (bounds[0] === bounds[1]) {
      padding = Math.max(Math.abs(low) * 0.08, 1);
      bounds = [clamp(low - padding), clamp(high + padding)];
    }
    return bounds;
  }
  function fraction(value, bounds) {
    if (value <= bounds[0]) return 0;
    if (value >= bounds[1]) return 1;
    const size = Math.max(Math.abs(bounds[0]), Math.abs(bounds[1]), 1);
    return Math.max(0, Math.min(1, (value / size - bounds[0] / size) / (bounds[1] / size - bounds[0] / size)));
  }
  // Match the archive's complete UTC grammar; Date is only a calendar validity check.
  // Ordering retains all six fractional digits rather than rounding to milliseconds.
  function utc(value) {
    const match = typeof value === "string" && /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value);
    const date = match && new Date(`${match[1]}Z`);
    if (!match || match[0] !== value || match[1].startsWith("0000-") ||
        !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== match[1]) return null;
    return `${match[1]}.${(match[2] || "").padEnd(6, "0")}`;
  }
  function valid(s, scope) {
    const start = utc(s?.interval?.started_at), end = utc(s?.interval?.ended_at);
    return s && typeof s.id === "string" && s.id.length && s.session_id === scope.session &&
      s.stream_id === scope.stream && s.level_unit === "dBm" &&
      Array.isArray(s.levels) && s.levels.length > 0 && s.levels.every(finite) &&
      Array.isArray(s.frequencies_hz) && s.frequencies_hz.length === s.levels.length &&
      s.frequencies_hz.every((n, i, a) => Number.isSafeInteger(n) && n >= 0 && (!i || n >= a[i - 1])) &&
      start !== null && end !== null && start <= end;
  }
  const gridKey = (s) => JSON.stringify([s.level_unit, s.frequencies_hz]);
  class View {
    constructor() { this.clear(); }
    clear() { this.key = null; this.live = []; this.display = []; this.bounds = null; this.paused = false; this.error = ""; }
    update(scope, sweeps) {
      if (!scope || ![scope.server, scope.session, scope.stream].every(v => typeof v === "string" && v.length) ||
          !Array.isArray(sweeps) || !sweeps.length || !sweeps.every(s => valid(s, scope))) { this.clear(); return; }
      const grid = gridKey(sweeps.at(-1));
      const key = JSON.stringify([scope.server, scope.session, scope.stream, grid]);
      if (this.key !== key) { this.clear(); this.key = key; }
      let start = sweeps.length - 1;
      while (start > 0 && gridKey(sweeps[start - 1]) === grid) start--;
      // Copy source data once into the bounded display domain, never annotate source objects.
      this.live = JSON.parse(JSON.stringify(sweeps.slice(Math.max(start, sweeps.length - CAPACITY))));
      if (!this.paused) this.display = this.live;
      if (!this.bounds) this.fit();
    }
    pause() { if (this.display.length) this.paused = true; }
    resume() { this.paused = false; this.display = this.live; }
    fit() { if (this.display.length) { this.bounds = fit(this.display.flatMap(s => s.levels)); this.error = ""; } }
    manual(lower, upper) {
      if (!finite(lower) || !finite(upper) || lower >= upper) { this.error = "Нужны конечные границы: min < max."; return false; }
      if (!this.display.length) return false;
      this.bounds = [lower, upper]; this.error = ""; return true;
    }
    clipped() { return this.bounds ? this.display.reduce((n, s) => n + s.levels.filter(v => v < this.bounds[0] || v > this.bounds[1]).length, 0) : 0; }
  }
  root.OpenRadioSpectrumView = { View, fit, fraction, CAPACITY };
  if (typeof module !== "undefined") module.exports = root.OpenRadioSpectrumView;
})(typeof window !== "undefined" ? window : globalThis);
