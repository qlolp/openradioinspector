import type { DisplaySweep } from './types';

/** Local display bookkeeping only; never serialized into a checkpoint or API. */
export interface PresentationReceipt {
  readonly epoch: object;
  readonly total: bigint;
}
export interface LiveSources {
  readonly rows: readonly DisplaySweep[];
  readonly presentation: PresentationReceipt;
}

/** Call only when committing sources to a renderer, never during React render. */
export class LiveSourceCursor {
  private currentEpoch?: object;
  private total = 0n;
  private evicted = 0n;

  get epoch() {
    return this.currentEpoch;
  }
  get presentationStatus() {
    return { retainedRowEvictions: this.evicted };
  }
  take({ rows, presentation }: LiveSources) {
    const reset = this.currentEpoch !== presentation.epoch;
    const previous = reset ? 0n : this.total;
    if (presentation.total < previous) throw Error('Presentation receipt moved backward');
    const appended = presentation.total - previous;
    const available = BigInt(rows.length);
    const retained = appended < available ? appended : available;
    this.currentEpoch = presentation.epoch;
    this.total = presentation.total;
    this.evicted = (reset ? 0n : this.evicted) + appended - retained;
    // Only a bounded pointer suffix is copied. Source DTOs and vectors stay owned
    // by the existing live buffer; seed/restored rows have no new-append count.
    return { reset, rows: retained ? rows.slice(-Number(retained)) : [] };
  }
}
