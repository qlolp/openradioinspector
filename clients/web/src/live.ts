import { FrameBuffer, SequenceGate, decimate, type Frame } from './pipeline';
import type { Connection } from './transport';
import type { DisplaySweep, Observation, Sweep } from './types';

export interface LiveSnapshot {
  rows: DisplaySweep[];
  observation?: Observation;
  sequence: number;
  state: string;
  gaps: number;
}
export class LiveStore {
  private buffer = new FrameBuffer(256);
  private gate = new SequenceGate();
  private worker = new Worker(new URL('./frame.worker.ts', import.meta.url), { type: 'module' });
  private listeners = new Set<() => void>();
  private cleanup?: () => void;
  private generation = 0;
  private pending: string[] = [];
  private processing = false;
  private raf = 0;
  private observation?: Observation;
  private receiver = '';
  private session = '';
  private receiverSession = '';
  private status = 'Connecting';
  private gaps = 0;
  private snapshot: LiveSnapshot = { rows: [], sequence: 0, state: 'Connecting', gaps: 0 };
  onGap: () => void = () => {};
  onInvalidate: (resources: string[]) => void = () => {};
  constructor(private transport: Connection) {
    this.worker.onmessage = (
      message: MessageEvent<{ generation: number; frame?: Frame; error?: string }>,
    ) => {
      if (message.data.generation !== this.generation) return;
      this.processing = false;
      if (message.data.error) this.gap('Invalid event — resynchronizing');
      else if (message.data.frame) this.apply(message.data.frame);
      this.pump();
    };
    this.worker.onerror = () => this.gap('Parser unavailable — reconnect to retry');
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  getSnapshot = () => this.snapshot;
  scope(session: string, stream: string, receiver: string, receiverSession = session) {
    if (receiverSession !== this.receiverSession || receiver !== this.receiver)
      this.observation = undefined;
    this.session = session;
    this.receiverSession = receiverSession;
    this.receiver = receiver;
    this.buffer.scope(session, stream);
    this.publish();
  }
  seed(sweeps: Sweep[], observations: Observation[]) {
    for (const sweep of sweeps) this.buffer.append(decimate(sweep));
    const observation = observations.at(-1);
    if (observation?.session_id === this.receiverSession && observation.stream_id === this.receiver)
      this.observation = observation;
    this.publish();
  }
  async start(after: number) {
    this.stop();
    this.gate.reset(after);
    this.status = 'Connecting';
    const generation = this.generation;
    const cleanup = await this.transport.subscribe(
      after,
      (raw) => {
        if (generation !== this.generation) return;
        if (this.pending.length >= 32) {
          this.gap('Render backlog — resynchronizing');
          return;
        }
        this.pending.push(raw);
        this.pump();
      },
      (connected) => {
        if (generation !== this.generation) return;
        this.status = connected
          ? 'Live · committed data'
          : 'Reconnecting · replaying committed events';
        this.publish();
      },
    );
    if (generation !== this.generation) cleanup();
    else this.cleanup = cleanup;
  }
  private pump() {
    if (this.processing || !this.pending.length) return;
    this.processing = true;
    this.worker.postMessage({ raw: this.pending.shift(), generation: this.generation });
  }
  private apply(frame: Frame) {
    const result = this.gate.accept(frame.sequence);
    if (result === 'duplicate') return;
    if (result === 'gap') {
      this.gap('Sequence gap — resynchronizing');
      return;
    }
    if (frame.sweep) this.buffer.append(frame.sweep);
    if (
      frame.observation?.session_id === this.receiverSession &&
      frame.observation.stream_id === this.receiver
    )
      this.observation = frame.observation;
    if (frame.invalidations.length) this.onInvalidate(frame.invalidations);
    this.publish();
  }
  private gap(reason: string) {
    this.stop();
    this.gaps++;
    this.status = reason;
    this.publish();
    this.onGap();
  }
  private publish() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      this.snapshot = {
        rows: this.buffer.rows,
        observation: this.observation,
        sequence: this.gate.last,
        state: this.status,
        gaps: this.gaps,
      };
      for (const listener of this.listeners) listener();
    });
  }
  stop() {
    this.cleanup?.();
    this.cleanup = undefined;
    this.generation++;
    this.pending = [];
    this.processing = false;
  }
  dispose() {
    this.stop();
    this.worker.terminate();
    cancelAnimationFrame(this.raf);
    this.listeners.clear();
  }
}
