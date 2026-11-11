export type EventSequence = number | bigint;
const MAX_SEQUENCE = 18_446_744_073_709_551_615n;
export function eventSequence(value: unknown, allowZero = false): EventSequence {
  if (!(typeof value === 'bigint' || (typeof value === 'number' && Number.isSafeInteger(value))))
    throw Error('Invalid event sequence');
  const exact = BigInt(value);
  if (exact < (allowZero ? 0n : 1n) || exact > MAX_SEQUENCE) throw Error('Invalid event sequence');
  return value;
}
