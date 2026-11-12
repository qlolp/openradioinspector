// Test-only, bounded passive observations of the real methods. No source,
// cursor, queue, generation, acknowledgement or animation scheduling is changed.
import { LiveStore } from '../src/live';
import { LiveSourceCursor } from '../src/live-presentation';
import { SpectrumRenderer } from '../src/spectrum-renderer';

export function captureSeedOrder() {
  const entries: unknown[] = [];
  const epochs = new WeakMap<object, number>();
  const originals: (() => void)[] = [];
  let serial = 0;
  let dropped = 0;
  const epoch = (value?: object) => {
    if (!value) return null;
    if (!epochs.has(value)) epochs.set(value, ++serial);
    return epochs.get(value);
  };
  const record = (value: unknown) => {
    if (entries.length === 2048) {
      dropped++;
      return;
    }
    entries.push({ at: performance.now(), value });
  };
  const storeState = (store: any) => ({
    cursor: String(store.gate.last),
    rows: store.buffer.rows.map((row: any) => row.id),
    provisional: store.provisional,
    epoch: epoch(store.presentation.epoch),
    total: String(store.presentation.total),
    raf: !!store.raf,
    session: store.session,
    stream: store.stream,
  });
  const graphState = (graph: any) => ({
    current: graph.sweep?.id ?? null,
    latest: graph.latest?.sweep?.id ?? null,
    pending: graph.pending.map((item: any) => item.sweep?.id ?? null),
    awaitingSource: graph.awaitingSource,
    releaseScheduled: graph.releaseScheduled,
    ownerGeneration: graph.ownerGeneration,
    sourceGeneration: graph.sourceGeneration,
    drawnSource: graph.drawnSource,
    canvasMarker: graph.canvas.dataset.sweepId ?? null,
    fallbackMarker: graph.fallback.dataset.sweepId ?? null,
  });
  function wrap(
    type: any,
    name: string,
    state: (value: any) => unknown,
    args: (values: any[]) => unknown = () => undefined,
  ) {
    const original = type.prototype[name];
    type.prototype[name] = function (...values: any[]) {
      record({
        method: type.name + '.' + name,
        phase: 'before',
        args: args(values),
        state: state(this),
      });
      const result = original.apply(this, values);
      record({
        method: type.name + '.' + name,
        phase: 'after',
        state: state(this),
        result:
          name === 'take'
            ? { reset: result.reset, rows: result.rows.map((row: any) => row.id) }
            : undefined,
      });
      return result;
    };
    originals.push(() => {
      type.prototype[name] = original;
    });
  }
  for (const method of ['scope', 'seed', 'apply', 'fencePresentation', 'publish', 'restore'])
    wrap(
      LiveStore,
      method,
      storeState,
      method === 'apply'
        ? ([frame]) => ({ sequence: String(frame.sequence), id: frame.sweep?.id ?? null })
        : method === 'seed'
          ? ([rows]) => rows.map((row: any) => row.id)
          : undefined,
    );
  wrap(
    LiveSourceCursor,
    'take',
    (cursor: any) => ({
      epoch: epoch(cursor.epoch),
      total: String(cursor.total),
      retainedRowEvictions: String(cursor.presentationStatus.retainedRowEvictions),
    }),
    ([value]) => ({
      epoch: epoch(value.presentation.epoch),
      total: String(value.presentation.total),
      rows: value.rows.map((row: any) => row.id),
    }),
  );
  for (const method of ['update', 'accept', 'invalidateCanvas', 'draw', 'dispose'])
    wrap(
      SpectrumRenderer,
      method,
      graphState,
      method === 'update'
        ? ([source]) => ({ id: source?.id ?? null })
        : method === 'accept'
          ? ([snapshot, newOwner]) => ({ id: snapshot.sweep?.id ?? null, newOwner })
          : undefined,
    );
  return {
    entries,
    get dropped() {
      return dropped;
    },
    record,
    close() {
      for (const restore of originals.reverse()) restore();
    },
  };
}
