import { afterEach, expect, it, vi } from 'vitest';
import { binaryStreamCapability, subscribeBinaryEvents } from '../src/websocket';
class Socket {
  static instances: Socket[] = [];
  binaryType = 'blob';
  readyState = 0;
  protocol = 'ori.measurements.v1.protobuf';
  onopen?: () => void;
  onmessage?: (event: { data: unknown }) => void;
  onclose?: (event: { code: number }) => void;
  onerror?: () => void;
  sent: string[] = [];
  closes: number[] = [];
  constructor(
    readonly url: string,
    readonly requested: string,
  ) {
    Socket.instances.push(this);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.({ data });
  }
  send(value: string) {
    this.sent.push(value);
  }
  close(code = 1000) {
    if (code !== 1000 && (code < 3000 || code > 4999))
      throw new DOMException('Reserved close code', 'InvalidAccessError');
    this.closes.push(code);
    this.readyState = 3;
    queueMicrotask(() => this.onclose?.({ code }));
  }
}
const capability = {
  version: 1,
  transport: 'websocket',
  encoding: 'protobuf',
  path: '/api/v1/events/ws',
  subprotocol: 'ori.measurements.v1.protobuf',
  max_frame_bytes: 16_842_752,
};
function setup() {
  Socket.instances = [];
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('location', new URL('https://radio.example/modern/'));
}
afterEach(() => vi.unstubAllGlobals());
it('does not downgrade a failed open that may be TLS or authentication failure', async () => {
  setup();
  const fallback = vi.fn(() => () => {}),
    disable = vi.fn();
  const stop = subscribeBinaryEvents(
    12,
    async () => 13,
    () => {},
    binaryStreamCapability({ features: { measurement_binary_stream: capability } })!,
    { fallback, disable },
  );
  try {
    Socket.instances[0].onerror?.();
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(2), { timeout: 2500 });
    expect(fallback).not.toHaveBeenCalled();
    expect(disable).not.toHaveBeenCalled();
    expect(Socket.instances[1].url).toContain('after=12');
  } finally {
    stop();
  }
});
it('negotiates only the exact bounded advertised v1 protocol', () => {
  expect(binaryStreamCapability({ features: {} })).toBeUndefined();
  expect(
    binaryStreamCapability({ features: { measurement_binary_stream: capability } })?.maxFrameBytes,
  ).toBe(16_842_752);
  for (const change of [
    { version: 2 },
    { path: 'https://elsewhere/api/v1/events/ws' },
    { subprotocol: 'token-secret' },
    { max_frame_bytes: 16_842_753 },
  ])
    expect(
      binaryStreamCapability({
        features: { measurement_binary_stream: { ...capability, ...change } },
      }),
    ).toBeUndefined();
});
it('awaits application before sending an exact decimal ACK and reconnects from the applied cursor', async () => {
  setup();
  let apply!: (value: bigint) => void;
  const received = vi.fn(
    () =>
      new Promise<bigint>((resolve) => {
        apply = resolve;
      }),
  );
  const fallback = vi.fn(() => () => {}),
    disable = vi.fn();
  const stop = subscribeBinaryEvents(
    9007199254740992n,
    received,
    () => {},
    binaryStreamCapability({ features: { measurement_binary_stream: capability } })!,
    { fallback, disable },
  );
  const first = Socket.instances[0];
  expect(first.url).toBe('wss://radio.example/api/v1/events/ws?after=9007199254740992');
  expect(first.requested).toBe(capability.subprotocol);
  expect(first.binaryType).toBe('arraybuffer');
  first.open();
  const data = new Uint8Array([1, 2, 3]).buffer;
  first.message(data);
  expect(received).toHaveBeenCalledWith(data);
  expect(first.sent).toEqual([]);
  // A disconnect while the worker applies must wait for that application, not
  // restart from an earlier delivered/queued cursor.
  first.close();
  apply(9007199254740993n);
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(2), { timeout: 2500 });
  expect(Socket.instances[1].url).toContain('after=9007199254740993');
  Socket.instances[1].open();
  Socket.instances[1].message(new ArrayBuffer(1));
  apply(9007199254740994n);
  await vi.waitFor(() => expect(Socket.instances[1].sent).toEqual(['9007199254740994']));
  expect(fallback).not.toHaveBeenCalled();
  expect(disable).not.toHaveBeenCalled();
  stop();
});
it('does not acknowledge malformed frames and falls back without moving the cursor', async () => {
  setup();
  const fallback = vi.fn(() => () => {}),
    disable = vi.fn();
  const stop = subscribeBinaryEvents(
    7,
    async () => {
      throw Error('Invalid protobuf');
    },
    () => {},
    binaryStreamCapability({ features: { measurement_binary_stream: capability } })!,
    { fallback, disable },
  );
  const socket = Socket.instances[0];
  socket.open();
  socket.message(new ArrayBuffer(1));
  await vi.waitFor(() => expect(fallback).toHaveBeenCalledWith(7n));
  expect(socket.sent).toEqual([]);
  expect(disable).toHaveBeenCalledOnce();
  stop();
});
it('cancels blocked application without ACK/reconnect and rejects a sender exceeding its one-frame window', async () => {
  setup();
  let apply!: (value: bigint) => void;
  const fallback = vi.fn(() => () => {}),
    disable = vi.fn();
  const stop = subscribeBinaryEvents(
    1,
    () =>
      new Promise<bigint>((resolve) => {
        apply = resolve;
      }),
    () => {},
    binaryStreamCapability({ features: { measurement_binary_stream: capability } })!,
    { fallback, disable },
  );
  const socket = Socket.instances[0];
  socket.open();
  socket.message(new ArrayBuffer(1));
  socket.message(new ArrayBuffer(1));
  expect(socket.closes).toContain(4008);
  stop();
  apply(2n);
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(socket.sent).toEqual([]);
  expect(fallback).not.toHaveBeenCalled();
  expect(Socket.instances).toHaveLength(1);
});
