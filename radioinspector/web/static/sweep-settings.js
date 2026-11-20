(() => {
  "use strict";

  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const metadata = key => key === "sequence" || key === "measurement_qualification";

  function jsonSetting(value, allowNull) {
    if (value === null) return allowNull;
    if (typeof value === "number") return Number.isFinite(value);
    if (typeof value === "boolean" || typeof value === "string") return true;
    if (Array.isArray(value)) return value.every(item => jsonSetting(item, allowNull));
    return object(value) && Object.values(value).every(item => jsonSetting(item, allowNull));
  }

  function valid(settings) {
    return object(settings) && Object.keys(settings).some(key => !metadata(key)) &&
      Object.entries(settings).every(([key, value]) => jsonSetting(value, metadata(key)));
  }

  function acquisition(settings) {
    return Object.fromEntries(Object.entries(settings).filter(([key]) => !metadata(key)));
  }

  window.oriSweepSettings = Object.freeze({ valid, acquisition });
})();
