import { useEffect, useId, useRef, useState } from 'react';
import {
  decodeRFExplorerBandPowerResult,
  decodeSweepToleranceResult,
  encodeSweepToleranceRequest,
  type JSONValue,
  type RFExplorerBandPowerResult,
  type SweepToleranceResult,
} from '../../../contracts/generated/typescript/index';
import { Api } from './api';
import { hz, utc } from './components';
import { t } from './i18n';
import { rfExplorerAdapter } from './rf-explorer';
import type { Sweep } from './types';
import { encodePathSegment, parseWire } from './wire';
import './RfExplorerResearch.css';

type Props = { api: Api; source: Sweep; features: Record<string, unknown> };
type FormProps = Pick<Props, 'api' | 'source'>;

function useAnalysis<T>(api: Api, source: Sweep) {
  const controller = useRef<AbortController | null>(null);
  const [result, setResult] = useState<T>();
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  function reset() {
    controller.current?.abort();
    controller.current = null;
    setPending(false);
    setError('');
    setResult(undefined);
  }
  useEffect(() => {
    reset();
    return () => controller.current?.abort();
  }, [api, source]);
  async function run(operation: (signal: AbortSignal) => Promise<T>) {
    reset();
    const active = new AbortController();
    controller.current = active;
    setPending(true);
    const timer = setTimeout(() => {
      if (active.signal.aborted) return;
      active.abort();
      setError(t('Analysis deadline exceeded'));
      setPending(false);
    }, 8000);
    try {
      const value = await operation(active.signal);
      if (!active.signal.aborted) setResult(value);
    } catch (cause) {
      if (!active.signal.aborted)
        setError(cause instanceof Error ? cause.message : t('Core request failed'));
    } finally {
      clearTimeout(timer);
      if (!active.signal.aborted) setPending(false);
    }
  }
  return { result, error, pending, run, reset };
}

function sameSource(actual: RFExplorerBandPowerResult['source'], source: Sweep) {
  return (
    actual.id === source.id &&
    actual.session_id === source.session_id &&
    actual.stream_id === source.stream_id &&
    actual.level_unit === source.level_unit &&
    actual.interval.started_at === source.interval.started_at &&
    actual.interval.ended_at === source.interval.ended_at &&
    actual.frequencies_hz.length === source.frequencies_hz.length &&
    actual.levels.length === source.levels.length &&
    actual.levels.every(
      (v, i) =>
        v === source.levels[i] &&
        BigInt(actual.frequencies_hz[i]) === BigInt(source.frequencies_hz[i]),
    )
  );
}

function BandPower({ api, source }: FormProps) {
  const [start, setStart] = useState(String(source.frequencies_hz[0]));
  const [stop, setStop] = useState(String(source.frequencies_hz.at(-1)));
  const { result, error, pending, reset, run } = useAnalysis<RFExplorerBandPowerResult>(
    api,
    source,
  );
  const valid =
    [start, stop].every((v) => /^\d+$/.test(v) && Number.isSafeInteger(Number(v))) &&
    Number(start) < Number(stop);
  return (
    <details className="analysis-panel">
      <summary>{t('Single-window band power')}</summary>
      <p>
        {t(
          'Choose two measured frequencies in this window. The core integrates the signal using the recorded RBW.',
        )}
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid)
            void run(async (signal) => {
              const data = await api.post<JSONValue>(
                '/sessions/' + encodePathSegment(source.session_id) + '/rf-explorer-band-power',
                { sweep_id: source.id, start_hz: Number(start), stop_hz: Number(stop) },
                signal,
              );
              const reply = decodeRFExplorerBandPowerResult(data);
              if (
                !sameSource(reply.source, source) ||
                Number(reply.start_hz) !== Number(start) ||
                Number(reply.stop_hz) !== Number(stop) ||
                Number(reply.rbw_hz) !== Number(source.settings.rbw_hz) ||
                reply.uncertainty_db !== null
              )
                throw Error(t('Source evidence mismatch'));
              return reply;
            });
        }}
      >
        <label>
          {t('Start frequency (Hz)')}
          <input
            name="start"
            inputMode="numeric"
            value={start}
            onChange={(e) => {
              reset();
              setStart(e.target.value);
            }}
          />
        </label>
        <label>
          {t('Stop frequency (Hz)')}
          <input
            name="stop"
            inputMode="numeric"
            value={stop}
            onChange={(e) => {
              reset();
              setStop(e.target.value);
            }}
          />
        </label>
        <button type="submit" disabled={pending || !valid}>
          {pending ? t('Executing') : t('Estimate band power')}
        </button>
      </form>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {result && (
        <div role="status">
          <strong>{result.value.toFixed(2)} dBm</strong> · RBW {hz(result.rbw_hz)}
          <p>
            {t('RBW-normalized estimate. Equivalent noise bandwidth is uncalibrated.')}{' '}
            {t('Uncertainty unknown')}
          </p>
        </div>
      )}
    </details>
  );
}

function TolerancePlot({ result }: { result: SweepToleranceResult }) {
  const { bins, target } = result;
  const first = Number(bins[0].frequency_hz);
  const last = Number(bins[bins.length - 1].frequency_hz);
  const minimum = Math.min(
    ...bins.map((bin, i) => Math.min(target.levels[i], bin.lower_dbm ?? target.levels[i])),
  );
  const maximum = Math.max(
    ...bins.map((bin, i) => Math.max(target.levels[i], bin.upper_dbm ?? target.levels[i])),
  );
  if (!Number.isFinite(maximum - minimum)) return null;
  const height = (level: number) =>
    maximum === minimum ? 50 : 3 + 94 * ((maximum - level) / (maximum - minimum));
  return (
    <figure className="rf-tolerance-plot">
      <small>{maximum.toFixed(1)} dBm</small>
      <div
        className="rf-tolerance-field"
        role="img"
        aria-label={t('Measured levels and reference limits')}
      >
        {bins.map((bin, i) => (
          <span
            key={i}
            className="rf-tolerance-column"
            style={{
              left:
                (last === first
                  ? 50
                  : (100 * (Number(bin.frequency_hz) - first)) / (last - first)) + '%',
            }}
          >
            {bin.upper_dbm != null && bin.lower_dbm != null && (
              <i
                data-tolerance-limit=""
                style={{
                  top: height(bin.upper_dbm) + '%',
                  height: height(bin.lower_dbm) - height(bin.upper_dbm) + '%',
                }}
              />
            )}
            <i
              data-measured-point={bin.classification}
              style={{ top: height(target.levels[i]) + '%' }}
            />
          </span>
        ))}
      </div>
      <small>{minimum.toFixed(1)} dBm</small>
      <div className="rf-tolerance-axis">
        <small>{hz(first)}</small>
        <small>{hz(last)}</small>
      </div>
      <figcaption>{t('Bars show reference limits. Dots show measured levels.')}</figcaption>
    </figure>
  );
}

function ReferenceEnvelope({ api, source, summaries }: FormProps & { summaries: boolean }) {
  const [refId, setRefId] = useState('');
  const [upper, setUpper] = useState('3');
  const [lower, setLower] = useState('3');
  const [open, setOpen] = useState(false);
  const [choices, setChoices] = useState<Pick<Sweep, 'id' | 'interval'>[]>([]);
  const listId = useId();
  const { result, error, pending, reset, run } = useAnalysis<SweepToleranceResult>(api, source);
  const path = '/sessions/' + encodePathSegment(source.session_id);
  useEffect(() => {
    if (!open || !summaries) return;
    const controller = new AbortController();
    const query = new URLSearchParams({
      stream_id: source.stream_id,
      limit: '32',
      min_hz: String(source.frequencies_hz[0]),
      max_hz: String(source.frequencies_hz.at(-1)),
    });
    void api
      .get<{ items: Sweep[] }>(path + '/sweep-summaries?' + query, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted)
          setChoices(
            page.items.filter(
              (s) =>
                s.id !== source.id &&
                s.session_id === source.session_id &&
                s.stream_id === source.stream_id &&
                s.level_unit === 'dBm',
            ),
          );
      })
      .catch(() => {
        if (!controller.signal.aborted) setChoices([]);
      });
    return () => controller.abort();
  }, [api, path, open, source, summaries]);
  const valid =
    !!refId.trim() &&
    [upper, lower].every((v) => v.trim() !== '' && Number.isFinite(Number(v)) && Number(v) >= 0);
  return (
    <details className="analysis-panel" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>{t('Reference tolerance envelope')}</summary>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid)
            void run(async (signal) => {
              const data = await api.post<JSONValue>(
                path + '/sweep-tolerance-envelope',
                parseWire(
                  encodeSweepToleranceRequest({
                    reference_id: refId,
                    target_id: source.id,
                    upper_deviation_db: Number(upper),
                    lower_deviation_db: Number(lower),
                  }),
                ),
                signal,
              );
              const reply = decodeSweepToleranceResult(data);
              if (
                !sameSource(reply.target, source) ||
                reply.session_id !== source.session_id ||
                reply.stream_id !== source.stream_id ||
                reply.reference.id !== refId ||
                reply.reference.session_id !== source.session_id ||
                reply.reference.stream_id !== source.stream_id ||
                reply.upper_deviation_db !== Number(upper) ||
                reply.lower_deviation_db !== Number(lower) ||
                reply.bins.length !== source.levels.length ||
                !reply.bins.every(
                  (bin, i) =>
                    Number(bin.target_index) === i &&
                    BigInt(bin.frequency_hz) === BigInt(source.frequencies_hz[i]) &&
                    (bin.classification === 'uncovered'
                      ? bin.reference_index == null &&
                        bin.lower_dbm == null &&
                        bin.upper_dbm == null
                      : bin.reference_index != null &&
                        reply.reference.frequencies_hz[Number(bin.reference_index)] ===
                          bin.frequency_hz &&
                        bin.lower_dbm != null &&
                        bin.upper_dbm != null),
                )
              )
                throw Error(t('Source evidence mismatch'));
              return reply;
            });
        }}
      >
        <label>
          {t('Reference sweep ID')}
          <input
            name="reference"
            list={listId}
            maxLength={128}
            value={refId}
            onChange={(e) => {
              reset();
              setRefId(e.target.value);
            }}
          />
        </label>
        <datalist id={listId}>
          {choices.map((s) => (
            <option key={s.id} value={s.id}>
              {utc(s.interval.started_at)}
            </option>
          ))}
        </datalist>
        <label>
          {t('Upper deviation (dB)')}
          <input
            name="upper"
            type="number"
            min="0"
            step="any"
            value={upper}
            onChange={(e) => {
              reset();
              setUpper(e.target.value);
            }}
          />
        </label>
        <label>
          {t('Lower deviation (dB)')}
          <input
            name="lower"
            type="number"
            min="0"
            step="any"
            value={lower}
            onChange={(e) => {
              reset();
              setLower(e.target.value);
            }}
          />
        </label>
        <button type="submit" disabled={pending || !valid}>
          {pending ? t('Executing') : t('Compare windows')}
        </button>
      </form>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {result && (
        <>
          <p role="status">
            {t('Above')} {String(result.counts.above)} · {t('Below')} {String(result.counts.below)}{' '}
            · {t('Within tolerance')} {String(result.counts.within)} · {t('Uncovered')}{' '}
            {String(result.counts.uncovered)}
          </p>
          <p>
            {t('Reference sweep ID')} {result.reference.id} ·{' '}
            {utc(result.reference.interval.started_at)} → {utc(result.reference.interval.ended_at)}
          </p>
          <TolerancePlot result={result} />
          <details>
            <summary>{t('Measured bins and limits')}</summary>
            <div className="original-table">
              <table>
                <thead>
                  <tr>
                    {[
                      'Frequency (Hz)',
                      'Lower limit (dBm)',
                      'Upper limit (dBm)',
                      'Measured level (dBm)',
                      'Classification',
                    ].map((label) => (
                      <th key={label}>{t(label)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.bins.map((bin, i) => (
                    <tr key={i}>
                      <td>{String(bin.frequency_hz)}</td>
                      <td>{bin.lower_dbm ?? '—'}</td>
                      <td>{bin.upper_dbm ?? '—'}</td>
                      <td>{source.levels[i]}</td>
                      <td>
                        {t(
                          {
                            above: 'Above',
                            below: 'Below',
                            within: 'Within tolerance',
                            uncovered: 'Uncovered',
                          }[bin.classification],
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </details>
  );
}

export function RfExplorerResearch({ api, source, features }: Props) {
  if (
    source.settings?.adapter !== rfExplorerAdapter ||
    source.level_unit !== 'dBm' ||
    !source.frequencies_hz.length
  )
    return null;
  return (
    <>
      {features.sweep_tolerance_envelope === true && (
        <ReferenceEnvelope
          api={api}
          source={source}
          summaries={features.sweep_summaries === true}
        />
      )}
      {features.rf_explorer_band_power === true && <BandPower api={api} source={source} />}
    </>
  );
}
