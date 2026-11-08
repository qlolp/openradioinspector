(() => {
  "use strict";

  // Presentation only. Sector polygons are computed by the core on the great
  // circle; the canvas draws an equirectangular view of the returned GeoJSON.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "editor", "source", "status", "summary", "items", "canvas"]
    .map(name => [name, byId(`map-${name}`)]));
  if (!dom.status) return;
  let scope = "", connected = true, suspended = false, result = null;
  let generation = 0, pending = null;
  const MAX_RESPONSE_BYTES = 2000000, MAX_MAP_BYTES = 131072;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  function request() {
    const raw = dom.editor.value;
    if (!raw.trim()) throw new Error("Вставьте карту измерений в черновик");
    if (raw.length > MAX_MAP_BYTES) throw new Error("Карта превышает 131 072 байт");
    return { map_json: raw };
  }
  function validate(data) {
    if (!object(data) || data.session_id !== scope || !object(data.geojson) ||
        data.geojson.type !== "FeatureCollection" || !Array.isArray(data.geojson.features) ||
        !object(data.summary) || !object(data.map)) {
      throw new Error("Ответ карты не соответствует сеансу");
    }
    const summary = data.summary;
    if (!Number.isSafeInteger(summary.point_count) || !Number.isSafeInteger(summary.sector_count) ||
        !object(summary.bbox)) {
      throw new Error("Некорректная сводка карты");
    }
    for (const feature of data.geojson.features) {
      if (!object(feature) || !object(feature.geometry) || !object(feature.properties)) {
        throw new Error("Некорректная фича GeoJSON");
      }
    }
    return data;
  }
  function controls() {
    dom.run.disabled = !(Boolean(scope) && connected && !suspended);
  }
  function clear() {
    pending?.abort(); pending = null; generation += 1; result = null;
    dom.summary.textContent = ""; dom.status.textContent = "";
    dom.items.replaceChildren();
    dom.canvas.hidden = true;
    controls();
  }
  function draw() {
    if (!result) return;
    const canvas = dom.canvas;
    canvas.hidden = false;
    const context = canvas.getContext("2d");
    context.fillStyle = "#07131c";
    context.fillRect(0, 0, canvas.width, canvas.height);
    const bbox = result.summary.bbox;
    const pad = 0.05;
    const spanLat = Math.max(bbox.max_latitude_deg - bbox.min_latitude_deg, 0.002) * (1 + pad);
    const spanLon = Math.max(bbox.max_longitude_deg - bbox.min_longitude_deg, 0.002) * (1 + pad);
    const midLat = (bbox.min_latitude_deg + bbox.max_latitude_deg) / 2;
    const midLon = (bbox.min_longitude_deg + bbox.max_longitude_deg) / 2;
    const project = (lat, lon) => [
      20 + ((lon - midLon) / spanLon + 0.5) * (canvas.width - 40),
      20 + (0.5 - (lat - midLat) / spanLat) * (canvas.height - 40),
    ];
    for (const feature of result.geojson.features) {
      if (feature.geometry.type === "Polygon") {
        const ring = feature.geometry.coordinates[0];
        context.beginPath();
        ring.forEach(([lon, lat], index) => {
          const [x, y] = project(lat, lon);
          if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
        });
        context.closePath();
        context.strokeStyle = "#ffb454";
        context.stroke();
      }
    }
    for (const feature of result.geojson.features) {
      if (feature.geometry.type === "Point") {
        const [lon, lat] = feature.geometry.coordinates;
        const [x, y] = project(lat, lon);
        context.fillStyle = "#6cd8ff";
        context.fillRect(x - 3, y - 3, 6, 6);
        context.fillStyle = "#9adfff";
        context.font = "11px sans-serif";
        context.fillText(feature.properties.label || "", x + 6, y - 4);
      }
    }
  }
  async function analyze() {
    if (dom.run.disabled) return;
    let body;
    try { body = request(); } catch (error) { clear(); dom.status.textContent = error.message; return; }
    const controller = new AbortController(); pending = controller; const current = ++generation;
    dom.run.disabled = true;
    dom.status.textContent = "Карта строится ядром…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/measurement-map`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body), cache: "no-store", signal: controller.signal,
      });
      const raw = await response.text();
      if (raw.length > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает безопасный размер");
      const envelope = JSON.parse(raw);
      if (response.status !== 200) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      const data = validate(envelope.data);
      if (current !== generation || suspended) return;
      result = data;
      draw();
      const rows = [];
      const line = document.createElement("li");
      line.textContent = `Точек: ${data.summary.point_count}, секторов: ${data.summary.sector_count}. Секторы построены ядром по дуге большого круга.`;
      rows.push(line);
      dom.items.replaceChildren(...rows);
      dom.summary.textContent = "Карта — производный артефакт от введённых координат: "
        + "это не измерение, не привязка GPS и не нормативная карта.";
      dom.status.textContent = "Карта построена. GeoJSON в черновике можно сохранить файлом.";
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Не удалось построить карту: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const sessionChanged = sessionId !== scope;
    scope = sessionId;
    if (sessionChanged) clear();
    dom.source.textContent = scope
      ? `Сеанс: ${scope}. Координаты вводятся явно.`
      : "Нет выбранного сеанса.";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) clear();
    controls();
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioMeasurementMap = { update, setConnected };
  clear();
})();
