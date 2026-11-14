import { useEffect, useRef, useState } from 'react';
import {
  decodeFieldStrengthCalibration,
  decodeFieldStrengthResult,
  encodeFieldStrengthCalibration,
  encodeFieldStrengthRequest,
  Float64,
  type FieldStrengthResult,
  type JSONValue,
} from '../../../contracts/generated/typescript/index';
import { Api, ResourceScope } from './api';
import { hz } from './components';
import { t } from './i18n';
import { encodePathSegment, parseWire, stringifyJSON } from './wire';

type Source = { id: string; session_id: string; stream_id: string; peak: { frequency_hz: number } };
// Compare the complete typed snapshot irrespective of JSON object key order.
function stable(value: unknown): string {
  if (value instanceof Float64) return stringifyJSON(value);
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object')
    return (
      '{' +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ':' + stable((value as Record<string, unknown>)[key]))
        .join(',') +
      '}'
    );
  return stringifyJSON(value);
}

export function FieldStrengthPanel({
  api,
  source,
  available,
  connectionVersion,
}: {
  api: Api;
  source?: Source;
  available: boolean;
  connectionVersion: number;
}) {
  const [draft, setDraft] = useState(''),
    [frequency, setFrequency] = useState('');
  const [pending, setPending] = useState(false),
    [error, setError] = useState('');
  const [result, setResult] = useState<FieldStrengthResult>();
  const owner = useRef(new ResourceScope());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  function invalidate() {
    owner.current.dispose();
    clearTimeout(timer.current);
    timer.current = undefined;
    setPending(false);
    setResult(undefined);
    setError('');
  }
  useEffect(() => {
    invalidate();
    setFrequency(source ? String(source.peak.frequency_hz) : '');
    return () => {
      owner.current.dispose();
      clearTimeout(timer.current);
    };
  }, [source, api, connectionVersion, available]);

  async function calculate() {
    if (pending) return;
    invalidate();
    try {
      if (!available) throw Error(t('Unavailable on this core'));
      if (!source) throw Error(t('Select an original sweep'));
      if (!/^\d+$/.test(frequency) || !Number.isSafeInteger(Number(frequency)))
        throw Error(t('Frequency must be integer Hz'));
      if (new TextEncoder().encode(draft).byteLength > 65536)
        throw Error(t('Calibration request exceeds 64 KiB'));
      const calibration = decodeFieldStrengthCalibration(parseWire(draft));
      const payload = { sweep_id: source.id, frequency_hz: Number(frequency), calibration };
      const body = parseWire(encodeFieldStrengthRequest(payload));
      if (new TextEncoder().encode(stringifyJSON(body)).byteLength > 65536)
        throw Error(t('Calibration request exceeds 64 KiB'));
      const scope = owner.current.next();
      setPending(true);
      timer.current = setTimeout(() => {
        if (!owner.current.current(scope.generation)) return;
        owner.current.dispose();
        timer.current = undefined;
        setPending(false);
        setError(t('Analysis deadline exceeded'));
      }, 6000);
      try {
        const value = await api.post<JSONValue>(
          '/sessions/' + encodePathSegment(source.session_id) + '/field-strength-rms-v2',
          body,
          scope.signal,
        );
        if (!owner.current.current(scope.generation)) return;
        const response = decodeFieldStrengthResult(value);
        if (
          response.source.id !== source.id ||
          response.source.session_id !== source.session_id ||
          response.source.stream_id !== source.stream_id ||
          BigInt(response.source.frequency_hz) !== BigInt(payload.frequency_hz) ||
          BigInt(response.calibration.coefficients.frequency_hz) !== BigInt(payload.frequency_hz) ||
          stable(parseWire(encodeFieldStrengthCalibration(response.calibration.profile))) !==
            stable(parseWire(encodeFieldStrengthCalibration(calibration))) ||
          !/^[0-9a-f]{64}$/.test(response.calibration.sha256)
        )
          throw Error(t('Source evidence mismatch'));
        setResult(response);
      } catch (failure) {
        if (owner.current.current(scope.generation))
          setError(failure instanceof Error ? failure.message : t('Core request failed'));
      } finally {
        if (owner.current.current(scope.generation)) {
          clearTimeout(timer.current);
          timer.current = undefined;
          setPending(false);
        }
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('Invalid calibration'));
    }
  }
  return (
    <details className="analysis-panel">
      <summary>{t('RMS-equivalent narrowband field')}</summary>
      <p className="muted">
        {t(
          'Operator-supplied calibration · RMS/CW only · core checks the recorded measurement chain. Legacy field_strength is an uncalibrated level estimate.',
        )}
      </p>
      {!available && <p role="status">{t('Unavailable on this core')}</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void calculate();
        }}
      >
        <label>
          {t('Frequency (Hz)')}
          <input
            inputMode="numeric"
            value={frequency}
            onChange={(event) => {
              invalidate();
              setFrequency(event.target.value);
            }}
          />
        </label>
        <label>
          {t('Calibration profile (JSON)')}
          <textarea
            rows={8}
            spellCheck={false}
            value={draft}
            onChange={(event) => {
              invalidate();
              setDraft(event.target.value);
            }}
          />
        </label>
        <div className="actions">
          <button type="submit" disabled={pending || !available || !source || !draft.trim()}>
            {pending ? t('Executing') : t('Estimate field')}
          </button>
          {pending && (
            <button type="button" onClick={invalidate}>
              {t('Cancel')}
            </button>
          )}
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && (
        <div data-testid="field-strength-result" aria-live="polite">
          <p className="mono">
            {result.value.toPrecision(6)} V/m · {result.value_dbuv_per_m.toFixed(3)} dBµV/m
          </p>
          <p>
            {result.uncertainty_db === null
              ? t('Uncertainty unknown')
              : `${result.uncertainty_db} dB`}
          </p>
          <p className="mono">
            {result.source.id} · {hz(result.source.frequency_hz)}
          </p>
          <p>
            {result.calibration.profile.id} · v{String(result.calibration.profile.version)}
          </p>
          <p className="mono">SHA-256: {result.calibration.sha256}</p>
          {result.warnings.map((warning, index) => (
            <p key={index}>{warning}</p>
          ))}
        </div>
      )}
    </details>
  );
}
