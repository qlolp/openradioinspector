import type { EventSequence } from './sequence';
import type { Health } from './types';

export interface HealthReceipt {
  readonly epoch: symbol;
  readonly request: bigint;
  readonly applied: bigint;
  readonly observed?: bigint;
}
export interface HealthCheckpoint {
  readonly value?: Health;
  readonly archiveChanged: boolean;
}

/** Local health samples, never an instruction or ownership receipt for Core. */
export class HealthReceipts {
  private epoch = Symbol();
  private issued = 0n;
  private accepted = 0n;
  private observed?: bigint;
  private latest?: Health;
  private backwards = false;

  constructor(checkpoint?: HealthCheckpoint) {
    if (checkpoint?.value) {
      this.latest = checkpoint.value;
      this.observed = BigInt(checkpoint.value.last_event_id);
      this.backwards = checkpoint.archiveChanged;
    }
  }
  checkpoint(): HealthCheckpoint {
    return { value: this.latest, archiveChanged: this.backwards };
  }

  capture(applied: EventSequence): HealthReceipt {
    return {
      epoch: this.epoch,
      request: ++this.issued,
      applied: BigInt(applied),
      observed: this.observed,
    };
  }
  current(receipt: HealthReceipt) {
    return receipt.epoch === this.epoch && receipt.request >= this.accepted;
  }
  accept(receipt: HealthReceipt, value: Health) {
    if (!this.current(receipt)) return false;
    const head = BigInt(value.last_event_id);
    const backwards =
      head < receipt.applied || (receipt.observed !== undefined && head < receipt.observed);
    // A concurrent request may have read a newer head after this request began.
    // Its lower sample is ambiguous, not evidence of rollback or a new UI head.
    if (!backwards && this.observed !== undefined && head < this.observed) return false;
    this.backwards ||= backwards;
    this.accepted = receipt.request;
    this.observed = head;
    this.latest = value;
    return true;
  }
  get value() {
    return this.latest;
  }
  get archiveChanged() {
    return this.backwards;
  }
  resetNode() {
    this.epoch = Symbol();
    this.accepted = this.issued;
    this.observed = undefined;
    this.latest = undefined;
    this.backwards = false;
  }
  acknowledgeArchive(value: Health) {
    this.resetNode();
    this.observed = BigInt(value.last_event_id);
    this.latest = value;
  }
}
