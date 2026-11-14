import type { EventSequence } from './sequence';
import {
  FrameBuffer,
  SequenceGate,
  prepareSweep,
  type Frame,
  type UnsupportedSweep,
} from './pipeline';
import type { Connection } from './transport';
import type { DisplaySweep, Observation, Sweep } from './types';
import type { PresentationReceipt } from './live-presentation';

export interface LiveSnapshot {
  rows: DisplaySweep[];
  presentation: PresentationReceipt;
  observation?: Observation;
  unsupported?: UnsupportedSweep;
  sequence: EventSequence;
  state: string;
  gaps: number;
}
export interface LiveCheckpoint {
  sequence: EventSequence;
  rows: DisplaySweep[];
  observation?: Observation;
  unsupported?: UnsupportedSweep;
  session: string;
  stream: string;
  receiver: string;
  receiverSession: string;
  seeded: boolean;
  provisional: boolean;
  gaps: number;
}
export class LiveStore {
  private buffer = new FrameBuffer(256);
  private gate = new SequenceGate();
  private worker?: Worker;
  private listeners = new Set<() => void>();
  private cleanup?: () => void;
  private generation = 0;
  private pending?: { resolve: (sequence: EventSequence) => void; reject: (reason: Error) => void };
  private disposed = false;
  private provisional = false;
  private seeded = false;
  private anchored = false;
  private raf = 0;
  private observation?: Observation;
  private unsupported?: UnsupportedSweep;
  private receiver = '';
  private session = '';
  private stream = '';
  private receiverSession = '';
  private status = 'Connecting';
  private gaps = 0;
  private presentation: PresentationReceipt = { epoch: {}, total: 0n };
  private snapshot: LiveSnapshot = {
    rows: [],
    presentation: this.presentation,
    sequence: 0,
    state: 'Connecting',
    gaps: 0,
  };
  onGap: () => void = () => {};
  onInvalidate: (resources: string[]) => void = () => {};
  constructor(private transport: Connection) {}
  private createWorker() {
    const worker = new Worker(new URL('./frame.worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (
      message: MessageEvent<{ generation: number; frame?: Frame; error?: string }>,
    ) => {
      if (message.data.generation !== this.generation || !this.pending) return;
      const pending = this.pending;
      this.pending = undefined;
      try {
        if (message.data.error || !message.data.frame)
          throw Error('Invalid event — resynchronizing');
        this.apply(message.data.frame);
        pending.resolve(message.data.frame.sequence);
      } catch (error) {
        pending.reject(error as Error);
        this.gap((error as Error).message);
      }
    };
    worker.onerror = () => {
      if (worker === this.worker && !this.disposed) {
        worker.terminate();
        this.worker = undefined;
        this.gap('Parser unavailable — reconnect to retry');
      }
    };
    return worker;
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  getSnapshot = () => this.snapshot;
  lastApplied = () => this.gate.last;
  isApplying = () => this.pending !== undefined;
  needsSeed = () => !this.seeded;
  checkpoint(): LiveCheckpoint | undefined {
    if (!this.anchored) return;
    return {
      sequence: this.gate.last,
      rows: [...this.buffer.rows],
      observation: this.observation,
      unsupported: this.unsupported,
      session: this.session,
      stream: this.stream,
      receiver: this.receiver,
      receiverSession: this.receiverSession,
      seeded: this.seeded,
      provisional: this.provisional,
      gaps: this.gaps,
    };
  }
  restore(value: LiveCheckpoint) {
    this.stop();
    this.fencePresentation();
    this.buffer.clear();
    this.scope(value.session, value.stream, value.receiver, value.receiverSession);
    this.gate.reset(value.sequence);
    this.anchored = true;
    for (const row of value.rows.slice(-256)) this.buffer.append(row);
    this.observation = value.observation;
    this.unsupported = value.unsupported;
    this.seeded = value.seeded;
    this.provisional = value.provisional;
    this.gaps = value.gaps;
    this.status = 'Reconnecting · replaying committed events';
    this.publish();
  }
  scope(session: string, stream: string, receiver: string, receiverSession = session) {
    if (session !== this.session || stream !== this.stream) {
      this.unsupported = undefined;
      this.fencePresentation();
    }
    if (
      session !== this.session ||
      stream !== this.stream ||
      receiver !== this.receiver ||
      receiverSession !== this.receiverSession
    )
      this.seeded = false;
    if (receiverSession !== this.receiverSession || receiver !== this.receiver)
      this.observation = undefined;
    this.session = session;
    this.stream = stream;
    this.receiverSession = receiverSession;
    this.receiver = receiver;
    this.buffer.scope(session, stream);
    this.publish();
  }
  seed(sweeps: Sweep[], observations: Observation[]) {
    this.seeded = true;
    if (!this.buffer.rows.length) {
      for (const sweep of sweeps) {
        const prepared = prepareSweep(sweep);
        if (prepared.sweep) this.buffer.append(prepared.sweep);
        if (prepared.unsupported) this.unsupported = prepared.unsupported;
      }
      this.provisional = this.buffer.rows.length > 0;
    }
    const observation = observations.at(-1);
    if (
      !this.observation &&
      observation?.session_id === this.receiverSession &&
      observation.stream_id === this.receiver
    )
      this.observation = observation;
    this.publish();
  }
  async start(after: EventSequence) {
    if (this.disposed) throw new DOMException('Cancelled', 'AbortError');
    this.stop();
    this.gate.reset(after);
    this.anchored = true;
    this.worker ??= this.createWorker();
    this.status = 'Connecting';
    const generation = this.generation;
    const cleanup = await this.transport.subscribe(
      after,
      (raw) => {
        if (generation !== this.generation || this.disposed)
          return Promise.reject(new DOMException('Cancelled', 'AbortError'));
        if (this.pending) return Promise.reject(Error('Concurrent event delivery is unsupported'));
        return new Promise<EventSequence>((resolve, reject) => {
          this.pending = { resolve, reject };
          try {
            this.worker!.postMessage({ raw, generation }, typeof raw === 'string' ? [] : [raw]);
          } catch {
            this.pending = undefined;
            reject(Error('Parser unavailable — reconnect to retry'));
            this.gap('Parser unavailable — reconnect to retry');
          }
        });
      },
      (connected, replayReason) => {
        if (generation !== this.generation) return;
        if (replayReason) {
          this.gap(replayReason);
          return;
        }
        this.status = connected
          ? 'Live · committed data'
          : 'Reconnecting · replaying committed events';
        this.publish();
      },
    );
    if (generation !== this.generation) cleanup();
    else this.cleanup = cleanup;
  }
  private apply(frame: Frame) {
    const result = this.gate.accept(frame.sequence);
    if (result === 'duplicate') return;
    if (result === 'gap') {
      throw Error('Sequence gap — resynchronizing');
    }
    if (frame.sweep) {
      if (frame.sweep.session_id === this.session && frame.sweep.stream_id === this.stream)
        this.unsupported = undefined;
      if (
        this.provisional &&
        frame.sweep.session_id === this.session &&
        frame.sweep.stream_id === this.stream
      ) {
        this.buffer.clear();
        this.provisional = false;
        this.fencePresentation();
      }
      const previous = this.buffer.rows;
      this.buffer.append(frame.sweep);
      if (this.buffer.rows !== previous)
        this.presentation = { epoch: this.presentation.epoch, total: this.presentation.total + 1n };
    }
    if (
      frame.unsupported?.session_id === this.session &&
      frame.unsupported.stream_id === this.stream
    ) {
      this.unsupported = frame.unsupported;
      // App unmounts the plot for an unsupported source. A later valid source
      // starts a new display baseline rather than replaying that old plot.
      this.fencePresentation();
    }
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
        presentation: this.presentation,
        observation: this.observation,
        unsupported: this.unsupported,
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
    const pending = this.pending;
    this.pending = undefined;
    pending?.reject(new DOMException('Cancelled', 'AbortError'));
    if (pending && !this.disposed) {
      this.worker?.terminate();
      this.worker = undefined;
    }
  }
  resetArchive(after: EventSequence = 0) {
    this.stop();
    this.fencePresentation();
    this.gate.reset(after);
    this.anchored = true;
    this.buffer.clear();
    this.provisional = false;
    this.seeded = false;
    this.observation = undefined;
    this.unsupported = undefined;
    this.status = 'Archive changed — display reset';
    this.publish();
  }
  private fencePresentation() {
    this.presentation = { epoch: {}, total: 0n };
  }
  dispose() {
    this.disposed = true;
    this.stop();
    this.worker?.terminate();
    cancelAnimationFrame(this.raf);
    this.listeners.clear();
  }
}
