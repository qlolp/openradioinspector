import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const CORE = 'http://127.0.0.1:18771/api/v1';
export const BINARY_CAPABILITY = {
  version: 1,
  transport: 'websocket',
  encoding: 'protobuf',
  path: '/api/v1/events/ws',
  subprotocol: 'ori.measurements.v1.protobuf',
  max_frame_bytes: 16_842_752,
};
export async function ownProject(request: APIRequestContext, name: string) {
  const template = (await (await request.get(CORE + '/sessions')).json()).data[0];
  const response = await request.post(CORE + '/sessions', {
    data: {
      name,
      streams: template.streams.map(({ id, session_id, state, ...stream }: any) => stream),
    },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const session = (await response.json()).data;
  const project = await request.post(CORE + '/monitoring-projects', {
    data: {
      name,
      site: 'Synthetic unarmed client protocol fixture',
      session_id: session.id,
    },
  });
  expect(project.ok(), await project.text()).toBe(true);
  return { session, project: (await project.json()).data };
}
export async function advertiseBinary(page: Page) {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch(),
      envelope = await response.json();
    envelope.data.features.measurement_binary_stream = BINARY_CAPABILITY;
    await route.fulfill({ response, json: envelope });
  });
}
/** Observe the real browser socket without routing frames through Playwright IPC. */
export async function observeBinary(page: Page, forceFirstDisconnect = false) {
  await page.addInitScript((disconnect) => {
    const Original = WebSocket;
    const stats = {
      cursors: [] as string[],
      protocols: [] as string[],
      acknowledged: [] as string[],
      opened: 0,
      frames: [] as { received: number; acknowledged?: number }[],
    };
    (window as any).__socket = stats;
    (window as any).WebSocket = new Proxy(Original, {
      construct(Target, args) {
        const socket = new Target(...(args as [string, string | string[]]));
        // Vite has its own HMR WebSocket; never count or interrupt that socket.
        if (new URL(String(args[0]), location.href).pathname !== '/api/v1/events/ws') return socket;
        stats.cursors.push(new URL(String(args[0])).searchParams.get('after')!);
        socket.addEventListener('open', () => {
          stats.opened++;
          stats.protocols.push(socket.protocol);
          if (disconnect && stats.opened === 1) socket.close();
        });
        socket.addEventListener('message', () => {
          if (stats.frames.length < 2048) stats.frames.push({ received: performance.now() });
        });
        const send = socket.send.bind(socket);
        socket.send = (message) => {
          if (typeof message !== 'string' || !/^[1-9][0-9]{0,19}$/.test(message))
            throw Error('Noncanonical binary ACK');
          stats.acknowledged.push(message);
          if (stats.frames.length) stats.frames.at(-1)!.acknowledged = performance.now();
          send(message);
        };
        return socket;
      },
    });
  }, forceFirstDisconnect);
}
