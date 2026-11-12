// Real embedded application: no routed HTTP responses or synthetic WS frames.
import {
  chromium,
  expect,
} from "../../clients/web/node_modules/@playwright/test/index.mjs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { hardwareIdentityForRealGLTests } from "../../clients/web/e2e/graphics-fixture.mjs";

const [vectors, directory] = process.argv.slice(2);
const fixture = JSON.parse(await readFile(vectors, "utf8"));
const rendererProfile = fixture.renderer_profile;
if (!["canvas", "webgl2"].includes(rendererProfile))
  throw Error("An explicit canvas or webgl2 rendering profile is required");
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || chromium.executablePath(),
  args:
    rendererProfile === "canvas"
      ? ["--no-sandbox", "--disable-webgl", "--disable-gpu"]
      : [
          "--no-sandbox",
          "--use-angle=swiftshader-webgl",
          "--enable-unsafe-swiftshader",
        ],
});
let page;
let profiler;
const requests = [];
try {
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  if (rendererProfile === "webgl2")
    await page.addInitScript(hardwareIdentityForRealGLTests);
  page.on("response", (response) => {
    if (requests.length < 128)
      requests.push([response.status(), new URL(response.url()).pathname]);
  });
  page.on("requestfailed", (request) => {
    if (requests.length < 128)
      requests.push([
        "failed",
        new URL(request.url()).pathname,
        request.failure(),
      ]);
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.addInitScript(
    ({ after, rendererProfile }) => {
      localStorage.setItem("ori-modern-locale", "en");
      const proof = {
        time_origin_ms: performance.timeOrigin,
        opened: 0,
        acks: [],
        sources: [],
        disconnected: false,
        connections: [],
        applied_sequences: [],
        ack_after_apply: [],
        binary_frames: 0,
        renderer_profile: rendererProfile,
        timings: {
          received: [],
          posted: [],
          callback: [],
          applied: [],
          ack: [],
        },
      };
      window.__interop = proof;
      const OriginalSocket = WebSocket;
      window.WebSocket = new Proxy(OriginalSocket, {
        construct(Target, args) {
          const socket = new Target(...args);
          const url = new URL(String(args[0]), location.href);
          if (url.pathname !== "/api/v1/events/ws") return socket;
          const connection = {
            after: url.searchParams.get("after"),
            applied_at_connect: proof.applied_sequences.at(-1) || after,
          };
          proof.connections.push(connection);
          socket.addEventListener("open", () => {
            proof.opened++;
            connection.protocol = socket.protocol;
            connection.binary_type = socket.binaryType;
          });
          socket.addEventListener("message", (event) => {
            if (event.data instanceof ArrayBuffer) proof.binary_frames++;
            proof.timings.received.push(performance.now());
            if (!proof.disconnected && proof.sources.length === 16) {
              proof.disconnected = true;
              socket.close();
            }
          });
          const send = socket.send.bind(socket);
          socket.send = (message) => {
            proof.timings.ack.push(performance.now());
            proof.acks.push(message);
            proof.ack_after_apply.push({
              sequence: message,
              applied: proof.applied_sequences.at(-1),
            });
            send(message);
          };
          return socket;
        },
      });
      const OriginalWorker = Worker;
      const set = Object.getOwnPropertyDescriptor(
        Worker.prototype,
        "onmessage",
      ).set;
      window.Worker = new Proxy(OriginalWorker, {
        construct(Target, args) {
          const worker = new Target(...args);
          const post = worker.postMessage.bind(worker);
          worker.postMessage = (...messages) => {
            if (messages[0] && Object.hasOwn(messages[0], "raw"))
              proof.timings.posted.push(performance.now());
            return post(...messages);
          };
          Object.defineProperty(worker, "onmessage", {
            set(handler) {
              set.call(worker, (event) => {
                const source = event.data.frame?.sweep;
                const callback = performance.now();
                handler.call(worker, event);
                if (source?.id.startsWith("interop-")) {
                  proof.timings.callback.push(callback);
                  proof.timings.applied.push(performance.now());
                  const bits = [...source.levels].map((value) => {
                    const bytes = new DataView(new ArrayBuffer(8));
                    bytes.setFloat64(0, value, false);
                    return bytes
                      .getBigUint64(0, false)
                      .toString(16)
                      .padStart(16, "0");
                  });
                  proof.sources.push({
                    id: source.id,
                    frequencies: [...source.frequencies_hz],
                    bits,
                    interval: source.interval,
                    unit: source.level_unit,
                  });
                  proof.applied_sequences.push(
                    String(event.data.frame.sequence),
                  );
                }
              });
            },
          });
          return worker;
        },
      });
    },
    { after: fixture.after, rendererProfile },
  );
  await page.goto(fixture.url + "/modern/");
  await page.getByTestId("legacy-compatibility-consent").check();
  await page.getByTestId("legacy-compatibility-continue").click();
  await page.getByTestId("project-select").selectOption(fixture.project_id);
  await expect
    .poll(() => page.evaluate(() => window.__interop.opened), {
      timeout: 10000,
    })
    .toBeGreaterThan(0);
  if (process.env.ORI_CLIENT_INTEROP_DIAGNOSTICS === "1") {
    profiler = await page.context().newCDPSession(page);
    await profiler.send("Profiler.enable");
    await profiler.send("Profiler.start");
  }
  for (const body of fixture.request_bodies) {
    const response = await fetch(
      fixture.url + "/api/v1/sessions/" + fixture.session_id + "/sweeps/batch",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      },
    );
    expect(response.status, await response.text()).toBe(201);
  }
  const final = (
    BigInt(fixture.after) + BigInt(fixture.sweeps.length)
  ).toString();
  await expect
    .poll(() => page.evaluate(() => window.__interop.acks.at(-1)), {
      timeout: 30000,
    })
    .toBe(final);
  await expect(page.locator("canvas.spectrum").first()).toHaveAttribute(
    "data-sweep-id",
    fixture.sweeps.at(-1).id,
  );
  const proof = await page.evaluate(() => window.__interop);
  proof.renderers = {
    spectrum: await page
      .getByTestId("spectrum-renderer-state")
      .first()
      .textContent(),
    waterfall: await page.getByTestId("renderer-state").first().textContent(),
  };
  proof.renderer_identity_fixture = await page.evaluate(
    () => window.__oriTestHardwareIdentityFixture ?? null,
  );
  expect(proof.sources.map((source) => source.id)).toEqual(
    fixture.sweeps.map((source) => source.id),
  );
  expect(proof.sources.map((source) => source.bits)).toEqual(
    fixture.sweeps.map(() => fixture.level_bits),
  );
  expect(proof.sources.map((source) => source.frequencies)).toEqual(
    fixture.sweeps.map((source) => source.frequencies_hz),
  );
  for (let index = 0; index < proof.sources.length; index++) {
    expect(
      proof.sources[index].interval,
      "producer UTC interval for source " + index,
    ).toEqual(fixture.expected_sweeps[index].interval);
    expect(proof.sources[index].unit, "original unit for source " + index).toBe(
      fixture.expected_sweeps[index].level_unit,
    );
  }
  if (fixture.sweeps.length > 17) {
    expect(proof.disconnected).toBe(true);
    expect(proof.opened).toBeGreaterThanOrEqual(2);
  }
  expect(errors).toEqual([]);
  await page.screenshot({ path: path.join(directory, "web.png") });
  await writeFile(
    path.join(directory, "web.json"),
    JSON.stringify(proof, null, 2) + "\n",
  );
} finally {
  if (profiler) {
    const { profile } = await profiler.send("Profiler.stop");
    const raw = JSON.stringify(profile);
    if (raw.length > 4 * 1024 * 1024)
      throw Error("Diagnostic CPU profile exceeded its byte bound");
    await writeFile(path.join(directory, "web-cpu-profile.json"), raw + "\n");
  }
  if (page) {
    await page.screenshot({ path: path.join(directory, "web-final.png") });
    await writeFile(
      path.join(directory, "web-diagnostics.json"),
      JSON.stringify(
        {
          requests,
          body: (await page.locator("body").innerText()).slice(0, 32768),
          state: await page.evaluate(() => window.__interop),
        },
        null,
        2,
      ),
    );
  }
  await browser.close();
}
