(() => {
  "use strict";
  const field = name => document.getElementById(`serial-session-${name}`);
  const form = field("form"), status = field("status"), submit = field("submit");
  const inputs = [field("name"), field("tinysa"), field("receiver")];
  let pending = false;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending) return;
    const [name, tinySA, receiver] = inputs.map(input => input.value.trim());
    if (!name || !tinySA || !receiver || tinySA === receiver ||
        tinySA.startsWith("sim://") || receiver.startsWith("sim://")) {
      status.textContent = "Укажите название и два разных физических порта компьютера ядра (не sim://).";
      return;
    }
    const payload = {name, streams: [
      {device: "tinySA Ultra", role: "spectrum_analyzer", level_unit: "dBm", port: tinySA,
        capabilities: ["sweep", "waterfall"], settings: {adapter: "tinysa-serial",
          start_hz: 88000000, stop_hz: 108000000, points: 201, rbw_hz: 10000, period_s: 0.25}},
      {device: "AR-DV1", role: "receiver", level_unit: "s_meter_count", port: receiver,
        capabilities: ["tune", "audio"], settings: {adapter: "ardv1-serial",
          frequency_hz: 145500000, mode: "FM", squelch: 10, period_s: 0.25}},
    ]};
    pending = true;
    for (const input of [...inputs, submit]) input.disabled = true;
    status.textContent = "Создаю профиль…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/v1/sessions", {
        method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
        body: JSON.stringify(payload), signal: controller.signal,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error?.message || `HTTP ${response.status}`);
      if (body.contract !== "openradioinspector-platform" || body.contract_version !== 1 ||
          typeof body.data?.id !== "string" || !body.data.id || body.data.state !== "draft") {
        throw new Error("Некорректный ответ ядра");
      }
      status.textContent = `Создан черновик ${body.data.id}. Выберите его в списке. Запуск сеанса и приборов выполняется отдельно.`;
      document.getElementById("refresh-button").click();
    } catch (error) {
      // POST may have succeeded before the response was lost. Never retry automatically.
      status.textContent = `${error.message}. Обновите список сеансов перед повтором: профиль мог быть создан.`;
    } finally {
      clearTimeout(timeout);
      pending = false;
      for (const input of [...inputs, submit]) input.disabled = false;
    }
  });
})();
