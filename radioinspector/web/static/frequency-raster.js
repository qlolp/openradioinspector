(() => {
  "use strict";

  // Presentation and consistency checks only. Table parsing and rounding live in the core.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "editor", "file", "template", "save", "draft", "source", "status", "summary", "items", "candidates"]
    .map(name => [name, byId(`raster-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = "", candidates = [], pending = null, generation = 0;
  let suspended = false, connected = true, result = null, fileGeneration = 0;
  const MAX_RESPONSE_BYTES = 2000000, MAX_RASTER_BYTES = 65536;
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const identity = value => typeof value === "string" && value.length > 0 && !/[\uD800-\uDFFF]/.test(value);
  const safeId = value => identity(value) && Array.from(value).length <= 128;
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  };
  const keys = (value, expected) => object(value) && equal(Object.keys(value).sort(), expected.slice().sort());
  function artifact(table, canonical = true) {
    if (!keys(table, ["format", "format_version", "name", "frequency_unit", "entries"]) ||
        table.format !== "openradioinspector-frequency-raster" || table.format_version !== 1 ||
        table.frequency_unit !== "Hz" || typeof table.name !== "string" || /\p{C}/u.test(table.name)) {
      throw new Error("Некорректный артефакт таблицы частотной сетки");
    }
    const name = table.name.normalize("NFC").trim();
    if ([...name].length < 1 || [...name].length > 80 || (canonical && name !== table.name) ||
        !Array.isArray(table.entries) || table.entries.length < 1 || table.entries.length > 64) {
      throw new Error("Некорректное имя или записи таблицы");
    }
    let previousStop = -1;
    const names = new Set();
    const entries = table.entries.map(entry => {
      if (!keys(entry, ["name", "start_hz", "stop_hz", "grid_step_hz"]) ||
          typeof entry.name !== "string" || /\p{C}/u.test(entry.name)) throw new Error("Некорректная запись таблицы");
      const entryName = entry.name.normalize("NFC").trim();
      if ([...entryName].length < 1 || [...entryName].length > 80 || (canonical && entryName !== entry.name)) {
        throw new Error("Некорректное имя записи таблицы");
      }
      if (names.has(entryName)) throw new Error("Имена записей таблицы не должны повторяться");
      names.add(entryName);
      const { start_hz: start, stop_hz: stop, grid_step_hz: step } = entry;
      if (!integer(start) || !integer(stop) || !Number.isSafeInteger(step) || start >= stop || stop > 9007199254740991) {
        throw new Error("Диапазон записи таблицы вне безопасных целых Hz");
      }
      if (step < 1 || step > stop - start) throw new Error("Шаг сетки записи некорректен");
      if (start <= previousStop) throw new Error("Диапазоны записей не должны пересекаться");
      previousStop = stop;
      return { name: entryName, start_hz: start, stop_hz: stop, grid_step_hz: step };
    });
    return { ...table, name, entries };
  }
  function acceptedDraft(raw) {
    const parsed = JSON.parse(raw), stack = [];
    for (const [token] of raw.matchAll(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\],:]/g)) {
      if (token === "{") stack.push({ keys: new Set(), key: true });
      else if (token === "[") stack.push(null);
      else if (token === "}" || token === "]") stack.pop();
      else if (token === "," && stack.at(-1)) stack.at(-1).key = true;
      else if (token[0] === '"' && stack.at(-1)?.key) {
        const frame = stack.at(-1), key = JSON.parse(token);
        if (frame.keys.has(key)) throw new Error("Повторный ключ в черновике таблицы");
        frame.keys.add(key); frame.key = false;
      }
    }
    return artifact(parsed, false);
  }
  function validate(data, request) {
    if (!object(data) || data.session_id !== request.sessionId || data.order !== "request" ||
        !Number.isSafeInteger(data.count) || !Number.isSafeInteger(data.matched_count) ||
        !Number.isSafeInteger(data.unmatched_count) || !Array.isArray(data.items) ||
        data.items.length !== data.count || !Array.isArray(request.candidateIds) ||
        data.count !== request.candidateIds.length ||
        data.matched_count + data.unmatched_count !== data.count) {
      throw new Error("Ответ округления не соответствует запросу или области");
    }
    const table = artifact(data.table);
    if (!equal(table, acceptedDraft(request.tableJson))) throw new Error("Таблица ответа не совпадает с отправленным черновиком");
    let matched = 0;
    data.items.forEach((item, index) => {
      if (!keys(item, ["candidate_id", "frequency_hz", "matched", "rounded_frequency_hz",
                       "delta_hz", "grid_step_hz", "entry_name"]) ||
          item.candidate_id !== request.candidateIds[index] || !integer(item.frequency_hz)) {
        throw new Error("Некорректная строка результата округления");
      }
      if (item.matched === true) {
        matched += 1;
        const entry = table.entries.find(candidate => candidate.name === item.entry_name);
        if (!entry || item.entry_name === null || !Number.isSafeInteger(item.rounded_frequency_hz) ||
            !Number.isSafeInteger(item.delta_hz) || item.grid_step_hz !== entry.grid_step_hz ||
            item.rounded_frequency_hz - item.frequency_hz !== item.delta_hz ||
            item.rounded_frequency_hz < entry.start_hz || item.rounded_frequency_hz > entry.stop_hz ||
            (item.rounded_frequency_hz - entry.start_hz) % entry.grid_step_hz !== 0) {
          throw new Error("Округлённая частота не лежит на сетке своей записи");
        }
      } else if (item.matched === false) {
        if (item.rounded_frequency_hz !== null || item.delta_hz !== null ||
            item.grid_step_hz !== null || item.entry_name !== null) {
          throw new Error("Несовпавшая частота не должна иметь округлённого значения");
        }
      } else throw new Error("Поле matched должно быть логическим");
    });
    if (matched !== data.matched_count) throw new Error("Счётчики совпадений ответа не сходятся");
  }
  function controls() {
    const ready = Boolean(scope) && connected && !suspended && candidates.length > 0 && chosenIds().length > 0;
    dom.candidates.disabled = suspended || !connected || candidates.length === 0;
    dom.run.disabled = Boolean(pending) || !ready || !dom.editor.value.trim();
    dom.save.disabled = !result || suspended;
    dom.template.disabled = !candidates.length || suspended;
  }
  function clear() {
    pending?.abort(); pending = null; generation += 1; fileGeneration += 1; result = null;
    dom.summary.textContent = ""; dom.status.textContent = "";
    dom.items.replaceChildren();
    controls();
  }
  function chosenIds() {
    return Array.from(dom.candidates.selectedOptions ?? [], option => option.value);
  }
  async function analyze() {
    if (pending || dom.run.disabled) return;
    const tableJson = dom.editor.value;
    const candidateIds = chosenIds();
    if (!candidateIds.length) { dom.status.textContent = "Выберите хотя бы одного кандидата."; return; }
    const controller = new AbortController(); pending = controller; const current = ++generation;
    dom.run.disabled = true;
    dom.status.textContent = "Округление выполняется ядром…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/frequency-rounding`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ table_json: tableJson, candidate_ids: candidateIds }),
        cache: "no-store", signal: controller.signal,
      });
      const raw = await response.text();
      if (raw.length > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает безопасный размер");
      const envelope = JSON.parse(raw);
      if (response.status !== 200) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      const data = envelope.data;
      validate(data, { sessionId: scope, tableJson, candidateIds });
      if (current !== generation || suspended) return;
      result = data;
      dom.summary.textContent = `Кандидатов: ${data.count}; совпало с диапазонами таблицы: ${data.matched_count}; вне таблицы: ${data.unmatched_count}. Исходные частоты не изменены.`;
      const rows = data.items.map(item => {
        const line = document.createElement("li");
        const rounded = item.matched
          ? `→ ${item.rounded_frequency_hz} Гц (Δ ${item.delta_hz >= 0 ? "+" : ""}${item.delta_hz} Гц, ${item.entry_name}, шаг ${item.grid_step_hz} Гц)`
          : "вне диапазонов таблицы, частота не округлялась";
        line.textContent = `${item.candidate_id}: ${item.frequency_hz} Гц ${rounded}`;
        return line;
      });
      dom.items.replaceChildren(...rows);
      dom.status.textContent = "Округление завершено. Значения производные: кандидаты в архиве не изменялись.";
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Не удалось выполнить округление: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const incoming = Array.isArray(next.candidates) ? next.candidates : [];
    const signature = JSON.stringify(incoming.map(item => [item.id, item.center_frequency_hz]));
    const sessionChanged = sessionId !== scope;
    if (!sessionChanged && signature === snapshot) { controls(); return; }
    scope = sessionId; snapshot = signature;
    if (sessionChanged) clear();
    candidates = incoming;
    const previous = new Set(chosenIds());
    const options = candidates.map(candidate => {
      const option = document.createElement("option");
      option.value = candidate.id;
      option.textContent = `${candidate.id} · ${candidate.center_frequency_hz} Гц`;
      option.selected = previous.has(candidate.id);
      return option;
    });
    dom.candidates.replaceChildren(...options);
    dom.source.textContent = candidates.length
      ? `Кандидатов сеанса: ${candidates.length}. Исходные центральные частоты берутся из архива.`
      : "Нет кандидатов выбранного сеанса.";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) clear();
    controls();
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  function edited() {
    clear(); dom.status.textContent = "Черновик изменён. Проверка и округление только по кнопке.";
  }
  dom.editor.addEventListener("input", edited);
  dom.candidates.addEventListener("change", () => { clear(); });
  dom.template.addEventListener("click", () => {
    if (!candidates.length) return;
    try {
      const first = Math.min(...candidates.map(item => item.center_frequency_hz));
      const last = Math.max(...candidates.map(item => item.center_frequency_hz));
      const start = Math.max(0, first - 1_000_000), stop = last + 1_000_000, step = 100_000;
      dom.editor.value = JSON.stringify({ format: "openradioinspector-frequency-raster", format_version: 1,
        name: "НЕНОРМАТИВНЫЙ пример 100 кГц", frequency_unit: "Hz",
        entries: [{ name: "пример", start_hz: start, stop_hz: stop, grid_step_hz: step }] }, null, 2);
      edited();
    } catch (error) { dom.status.textContent = error.message; }
  });
  dom.file.addEventListener("change", async () => {
    const file = dom.file.files?.[0]; dom.file.value = "";
    if (!file || suspended) return;
    clear(); const current = fileGeneration;
    dom.status.textContent = "Чтение локального UTF-8 файла…";
    try {
      if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > MAX_RASTER_BYTES) throw new Error("Файл превышает 65 536 байт");
      const bytes = await file.slice(0, MAX_RASTER_BYTES + 1).arrayBuffer();
      if (current !== fileGeneration || suspended) return;
      if (bytes.byteLength !== file.size || bytes.byteLength > MAX_RASTER_BYTES) throw new Error("Файл прочитан не полностью или превышает 65 536 байт");
      dom.editor.value = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      dom.status.textContent = "Локальный файл загружен как непроверенный черновик. Округление только по кнопке.";
    } catch (error) {
      if (current === fileGeneration) dom.status.textContent = `Не удалось загрузить таблицу: ${error.message}. Прежний черновик сохранён.`;
    }
  });
  dom.save.addEventListener("click", () => {
    if (!result || suspended) return;
    let url, link;
    try {
      const canonical = JSON.stringify(result.table, null, 2) + "\n";
      url = URL.createObjectURL(new Blob([canonical], { type: "application/json;charset=utf-8" }));
      link = document.createElement("a"); link.href = url; link.download = "frequency-raster.json";
      document.body.appendChild(link); link.click();
      dom.status.textContent = `Передана браузеру проверенная таблица «${result.table.name}». Завершение сохранения определяется браузером.`;
    } catch (error) { dom.status.textContent = `Не удалось сохранить таблицу: ${error.message}`; }
    finally { link?.remove(); if (url) window.setTimeout(() => URL.revokeObjectURL(url), 0); }
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioFrequencyRaster = { update, setConnected };
  clear();
})();
