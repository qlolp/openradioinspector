/* OpenRadioInspector — веб-интерфейс.
   Отрисовка выполняется на canvas без внешних библиотек. */

"use strict";

const state = {
  data: null,
  waterfall: null,
  selection: null,
  version: -1,
};

const COLORS = {
  current: "#4ea3ff",
  max: "#ffb454",
  min: "#6fd3a0",
  avg: "#c792ea",
  thr: "#ff5f6d",
  grid: "#222b36",
  axis: "#3a4757",
  text: "#8b98a9",
};

function $(id) {
  return document.getElementById(id);
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined || !isFinite(value)) return "—";
  return Number(value).toFixed(digits);
}

function fmtTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  return d.toLocaleString("ru-RU");
}

/* ---------------------------------------------------------------- выборка */

function canvasPos(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) * (canvas.width / rect.width);
  const y = (event.clientY - rect.top) * (canvas.height / rect.height);
  return { x, y };
}

/* --------------------------------------------------------------- спектр */

function drawGrid(ctx, w, h, pad) {
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 10; i++) {
    const y = pad.t + ((h - pad.t - pad.b) * i) / 10;
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
  }
  for (let i = 0; i <= 10; i++) {
    const x = pad.l + ((w - pad.l - pad.r) * i) / 10;
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, h - pad.b);
  }
  ctx.stroke();
}

function drawTrace(ctx, trace, w, h, pad, color, loDb, hiDb, fLo, fHi) {
  if (!trace || !trace.frequencies.length) return;
  const freqs = trace.frequencies;
  const levels = trace.levels;
  const xMin = pad.l;
  const xMax = w - pad.r;
  const yMin = h - pad.b;
  const yMax = pad.t;

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (let i = 0; i < freqs.length; i++) {
    const f = freqs[i];
    if (f < fLo || f > fHi) continue;
    const x = xMin + ((f - fLo) / (fHi - fLo || 1)) * (xMax - xMin);
    let db = levels[i];
    if (db < loDb) db = loDb;
    if (db > hiDb) db = hiDb;
    const y = yMin - ((db - loDb) / (hiDb - loDb || 1)) * (yMin - yMax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function drawSpectrum() {
  const canvas = $("spectrum");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const pad = { l: 56, r: 12, t: 12, b: 26 };
  ctx.clearRect(0, 0, w, h);
  drawGrid(ctx, w, h, pad);

  const data = state.data;
  if (!data || !data.current) {
    ctx.fillStyle = COLORS.text;
    ctx.font = "13px sans-serif";
    ctx.fillText("Нет данных — загрузите базу данных панорам", pad.l, pad.t + 24);
    return;
  }

  const current = data.current;
  let loDb = Infinity;
  let hiDb = -Infinity;
  const consider = (tr) => {
    if (!tr) return;
    for (const v of tr.levels) {
      if (v < loDb) loDb = v;
      if (v > hiDb) hiDb = v;
    }
  };
  consider(current);
  if ($("show-max").checked) consider(data.max);
  if ($("show-min").checked) consider(data.min);
  if ($("show-avg").checked) consider(data.average);
  if (!isFinite(loDb) || !isFinite(hiDb)) return;
  const margin = Math.max(6, (hiDb - loDb) * 0.08);
  loDb -= margin;
  hiDb += margin;

  const fLo = current.frequencies[0];
  const fHi = current.frequencies[current.frequencies.length - 1];

  if ($("show-max").checked) drawTrace(ctx, data.max, w, h, pad, COLORS.max, loDb, hiDb, fLo, fHi);
  if ($("show-min").checked) drawTrace(ctx, data.min, w, h, pad, COLORS.min, loDb, hiDb, fLo, fHi);
  if ($("show-avg").checked) drawTrace(ctx, data.average, w, h, pad, COLORS.avg, loDb, hiDb, fLo, fHi);

  const thr = data.threshold;
  if (thr && thr.frequencies.length && $("show-threshold").checked) {
    ctx.strokeStyle = COLORS.thr;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < thr.frequencies.length; i++) {
      const f = thr.frequencies[i];
      if (f < fLo || f > fHi) { started = false; continue; }
      const x = pad.l + ((f - fLo) / (fHi - fLo || 1)) * (w - pad.l - pad.r);
      let db = thr.levels[i];
      if (db < loDb) db = loDb;
      if (db > hiDb) db = hiDb;
      const y = h - pad.b - ((db - loDb) / (hiDb - loDb || 1)) * (h - pad.t - pad.b);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  drawTrace(ctx, current, w, h, pad, COLORS.current, loDb, hiDb, fLo, fHi);

  // Выделенная полоса измерения.
  if (state.selection) {
    const x1 = pad.l + ((state.selection[0] - fLo) / (fHi - fLo || 1)) * (w - pad.l - pad.r);
    const x2 = pad.l + ((state.selection[1] - fLo) / (fHi - fLo || 1)) * (w - pad.l - pad.r);
    ctx.fillStyle = "rgba(78,163,255,0.14)";
    ctx.fillRect(Math.min(x1, x2), pad.t, Math.abs(x2 - x1), h - pad.t - pad.b);
  }

  // Оси.
  ctx.fillStyle = COLORS.text;
  ctx.font = "10px sans-serif";
  ctx.fillText(`${(hiDb).toFixed(0)} дБ`, 6, pad.t + 8);
  ctx.fillText(`${(loDb).toFixed(0)} дБ`, 6, h - pad.b);
  for (let i = 0; i <= 5; i++) {
    const f = fLo + ((fHi - fLo) * i) / 5;
    const x = pad.l + ((w - pad.l - pad.r) * i) / 5;
    ctx.fillText(`${(f / 1e6).toFixed(2)}`, x - 12, h - 10);
  }
  ctx.fillText("МГц", w - 26, h - 10);
}

/* ------------------------------------------------------------ спектрограмма */

function drawWaterfall() {
  const canvas = $("waterfall");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const wf = state.waterfall;
  if (!wf || !wf.levels || wf.levels.length === 0) {
    ctx.fillStyle = COLORS.text;
    ctx.font = "13px sans-serif";
    ctx.fillText("Спектрограмма недоступна: база данных пуста", 12, 24);
    return;
  }

  const grid = wf.levels;
  const rows = grid.length;
  const cols = grid[0].length;
  const img = ctx.createImageData(cols, rows);

  let lo = Infinity;
  let hi = -Infinity;
  for (const row of grid) {
    for (const v of row) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  const span = hi - lo || 1;

  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const t = (grid[r][c] - lo) / span;
      const idx = (r * cols + c) * 4;
      // Сине-жёлто-красная шкала.
      img.data[idx] = Math.min(255, 255 * Math.max(0, (t - 0.55) * 2.2));
      img.data[idx + 1] = Math.min(255, 255 * Math.max(0, 1 - Math.abs(t - 0.5) * 2.6));
      img.data[idx + 2] = Math.min(255, 255 * Math.max(0, 1.2 - t * 2.0));
      img.data[idx + 3] = 255;
    }
  }

  const tmp = document.createElement("canvas");
  tmp.width = cols;
  tmp.height = rows;
  tmp.getContext("2d").putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(tmp, 0, 0, w, h);

  ctx.fillStyle = COLORS.text;
  ctx.font = "10px sans-serif";
  ctx.fillText(`${hi.toFixed(0)} дБ`, 6, 12);
  ctx.fillText(`${lo.toFixed(0)} дБ`, 6, h - 6);
}

/* ------------------------------------------------------------------ карта */

function drawMap() {
  const canvas = $("map");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const data = state.data;
  const points = (data && data.points) || [];
  const note = $("map-note");
  if (!points.length) {
    ctx.fillStyle = COLORS.text;
    ctx.font = "13px sans-serif";
    ctx.fillText("Нет измерений с географическими координатами", 12, 24);
    note.textContent = "";
    return;
  }

  let latMin = Infinity, latMax = -Infinity, lonMin = Infinity, lonMax = -Infinity;
  let lo = Infinity, hi = -Infinity;
  for (const p of points) {
    latMin = Math.min(latMin, p.latitude);
    latMax = Math.max(latMax, p.latitude);
    lonMin = Math.min(lonMin, p.longitude);
    lonMax = Math.max(lonMax, p.longitude);
    lo = Math.min(lo, p.level_db);
    hi = Math.max(hi, p.level_db);
  }
  const pad = 24;
  const spanLat = latMax - latMin || 1e-4;
  const spanLon = lonMax - lonMin || 1e-4;

  ctx.strokeStyle = COLORS.grid;
  ctx.strokeRect(pad, pad, w - 2 * pad, h - 2 * pad);

  for (const p of points) {
    const x = pad + ((p.longitude - lonMin) / spanLon) * (w - 2 * pad);
    const y = h - pad - ((p.latitude - latMin) / spanLat) * (h - 2 * pad);
    const t = (p.level_db - lo) / (hi - lo || 1);
    const r = 4 + t * 5;
    ctx.fillStyle = `rgba(${Math.round(60 + t * 195)}, ${Math.round(160 - t * 110)}, ${Math.round(255 - t * 190)}, 0.85)`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.fillStyle = COLORS.text;
  ctx.font = "10px sans-serif";
  ctx.fillText(`${latMin.toFixed(5)} … ${latMax.toFixed(5)} с.ш.`, pad, h - 8);
  ctx.fillText(`${lonMin.toFixed(5)} … ${lonMax.toFixed(5)} в.д.`, w - 190, h - 8);
  ctx.fillText(`уровень ${lo.toFixed(0)}…${hi.toFixed(0)} дБ`, pad, 16);
  note.textContent = `Точек измерений: ${points.length}`;
}

/* ---------------------------------------------------------------- таблица */

function renderSignals() {
  const tbody = $("signal-table").querySelector("tbody");
  tbody.innerHTML = "";
  const signals = (state.data && state.data.signals) || [];
  signals.forEach((s, index) => {
    const tr = document.createElement("tr");
    if (s.from_assignment) tr.className = "matched";
    const cells = [
      String(index + 1),
      (s.frequency_hz / 1e6).toFixed(6),
      (s.bandwidth_hz / 1e3).toFixed(2),
      String(s.event_count),
      s.power_over_threshold_w.toExponential(3),
      fmtTime(s.first_seen),
    ];
    cells.forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

/* ------------------------------------------------------------- измерения */

async function runMeasurement() {
  const start = parseFloat($("m-start").value) * 1e6;
  const stop = parseFloat($("m-stop").value) * 1e6;
  const box = $("measure-result");
  box.innerHTML = "";
  if (!isFinite(start) || !isFinite(stop) || start >= stop) {
    $("measure-note").textContent = "Укажите корректные границы поддиапазона, МГц";
    return;
  }
  $("measure-note").textContent = "Выполняется измерение…";
  try {
    const response = await fetch(`/api/measure?start=${start}&stop=${stop}`);
    const data = await response.json();
    renderMeasurements(data);
    $("measure-note").textContent = "";
  } catch (err) {
    $("measure-note").textContent = `Ошибка запроса: ${err}`;
  }
}

async function runOccupancy() {
  const box = $("occ-result");
  box.innerHTML = "";
  const raw = $("occ-threshold").value;
  const query = raw === "" ? "" : `?threshold=${parseFloat(raw)}`;
  $("occ-note").textContent = "Выполняется оценка…";
  try {
    const response = await fetch(`/api/occupancy${query}`);
    const data = await response.json();
    renderOccupancy(data);
    $("occ-note").textContent = "";
  } catch (err) {
    $("occ-note").textContent = `Ошибка запроса: ${err}`;
  }
}

function renderOccupancy(data) {
  const box = $("occ-result");
  box.innerHTML = "";
  if (!data || !data.channel_count) {
    box.textContent = "Нет данных для оценки загрузки.";
    return;
  }
  const cards = [
    {
      label: "Загрузка диапазона",
      value: (data.band_occupancy * 100).toFixed(2) + " %",
    },
    {
      label: "Занятых каналов",
      value: `${data.occupied_channels} из ${data.channel_count}`,
    },
    {
      label: "Порог оценки",
      value: fmt(data.threshold_db, 1) + " дБ",
    },
    {
      label: "Время наблюдения",
      value: data.observation_time_s > 0
        ? (data.observation_time_s / 60).toFixed(1) + " мин"
        : "—",
    },
  ];
  for (const card of cards) {
    const div = document.createElement("div");
    div.className = "result-card";
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = card.label;
    const value = document.createElement("div");
    value.className = "value";
    value.textContent = card.value;
    div.appendChild(label);
    div.appendChild(value);
    box.appendChild(div);
  }
}

function renderMeasurements(data) {
  const box = $("measure-result");
  box.innerHTML = "";
  if (!data || !data.occupied_bandwidth_beta2) {
    box.textContent = "Нет данных для измерения.";
    return;
  }
  const cards = [
    {
      label: `Полоса β/2 (${data.occupied_bandwidth_beta2.method})`,
      value: (data.occupied_bandwidth_beta2.bandwidth_hz / 1e3).toFixed(3) + " кГц",
      warn: (data.occupied_bandwidth_beta2.warnings || []).join("; "),
    },
    {
      label: `Полоса по уровню ${data.bandwidth_at_level.method}`,
      value: (data.bandwidth_at_level.bandwidth_hz / 1e3).toFixed(3) + " кГц",
      warn: (data.bandwidth_at_level.warnings || []).join("; "),
    },
    {
      label: "Напряжённость поля",
      value:
        typeof data.field_strength.value === "number" &&
        isFinite(data.field_strength.value)
          ? fmt(data.field_strength.value, 2) + " дБ"
          : "—",
      warn: (data.field_strength.warnings || []).join("; "),
    },
    {
      label: "Центральная частота",
      value:
        typeof data.center_frequency_centroid.value === "number" &&
        isFinite(data.center_frequency_centroid.value)
          ? (data.center_frequency_centroid.value / 1e6).toFixed(6) + " МГц"
          : "—",
      warn: (data.center_frequency_centroid.warnings || []).join("; "),
    },
  ];
  for (const card of cards) {
    const div = document.createElement("div");
    div.className = "result-card";
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = card.label;
    const value = document.createElement("div");
    value.className = "value";
    value.textContent = card.value;
    div.appendChild(label);
    div.appendChild(value);
    if (card.warn) {
      const warn = document.createElement("div");
      warn.className = "warn";
      warn.textContent = card.warn;
      div.appendChild(warn);
    }
    box.appendChild(div);
  }
}

/* -------------------------------------------------------------- обновление */

function applyState(data) {
  state.data = data;
  if (data.waterfall && data.waterfall.levels && data.waterfall.levels.length) {
    state.waterfall = data.waterfall;
  }
  $("dbname").textContent = data.database || "база данных не загружена";
  $("panocount").textContent = `панорам: ${data.panoramas || 0}`;
  const settings = data.settings;
  if (settings) {
    if (!$("m-start").value || parseFloat($("m-start").value) === 0) {
      $("m-start").value = (settings.start_hz / 1e6).toFixed(3);
    }
    if (!$("m-stop").value || parseFloat($("m-stop").value) === 0) {
      $("m-stop").value = (settings.stop_hz / 1e6).toFixed(3);
    }
  }
  drawSpectrum();
  drawWaterfall();
  drawMap();
  renderSignals();
}

async function refresh() {
  try {
    const response = await fetch("/api/state");
    applyState(await response.json());
  } catch (err) {
    $("conn").textContent = "соединение: ошибка";
    $("conn").className = "off";
  }
}

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onmessage = (event) => {
    $("conn").textContent = "соединение: активно";
    $("conn").className = "on";
    try {
      applyState(JSON.parse(event.data));
    } catch (err) {
      /* повреждённое сообщение игнорируется */
    }
  };
  source.onerror = () => {
    $("conn").textContent = "соединение: переподключение";
    $("conn").className = "off";
  };
}

/* ------------------------------------------------------------- обработчики */

document.addEventListener("DOMContentLoaded", () => {
  refresh().then(connectStream).then(runOccupancy);

  const spectrum = $("spectrum");
  let dragging = false;

  const pickFrequency = (event) => {
    const data = state.data;
    if (!data || !data.current) return null;
    const pos = canvasPos(spectrum, event);
    const rect = spectrum.getBoundingClientRect();
    const pad = { l: 56, r: 12 };
    const rel = pos.x / (spectrum.width / rect.width);
    const frac = (rel * rect.width - pad.l) / (rect.width - pad.l - pad.r);
    const freqs = data.current.frequencies;
    const fLo = freqs[0];
    const fHi = freqs[freqs.length - 1];
    return fLo + frac * (fHi - fLo);
  };

  spectrum.addEventListener("mousedown", (event) => {
    const f = pickFrequency(event);
    if (f === null) return;
    dragging = true;
    state.selection = [f, f];
  });

  spectrum.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const f = pickFrequency(event);
    if (f === null) return;
    state.selection[1] = f;
    drawSpectrum();
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    if (state.selection) {
      state.selection.sort((a, b) => a - b);
      $("m-start").value = (state.selection[0] / 1e6).toFixed(3);
      $("m-stop").value = (state.selection[1] / 1e6).toFixed(3);
    }
    drawSpectrum();
  });

  $("do-measure").addEventListener("click", runMeasurement);
  $("do-occupancy").addEventListener("click", runOccupancy);

  $("thr-offset").addEventListener("input", (event) => {
    $("thr-value").textContent = parseFloat(event.target.value).toFixed(1);
  });

  $("apply-threshold").addEventListener("click", async () => {
    const offset = parseFloat($("thr-offset").value) || 0;
    try {
      await fetch("/api/threshold", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offset_db: offset }),
      });
      $("thr-offset").value = 0;
      $("thr-value").textContent = "0.0";
      await refresh();
    } catch (err) {
      $("thr-value").textContent = "ошибка";
    }
  });

  // Полный снимок обновляется периодически: поток событий передаёт
  // оперативные данные, а спектрограмма обновляется раз в 15 секунд.
  setInterval(refresh, 15000);

  ["show-max", "show-min", "show-avg", "show-threshold"].forEach((id) => {
    $(id).addEventListener("change", drawSpectrum);
  });
});
