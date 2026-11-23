import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, type Api } from './api';
import { hz, Modal, utc } from './components';
import { t } from './i18n';
import { useArchiveRead } from './RfExplorerOriginals';
import { SourceAge } from './RfExplorerPass';
import { frequencyAt, measuredRuns, type ReceiverPass } from './receiver-panorama';
import { formatQuantity } from './station';
import { encodePathSegment, originalObservationJSON } from './wire';
import type { Observation } from './types';
import { message, type Desk } from './useDesk';

export function OriginalObservation({
  source,
  close,
  children,
}: {
  source: Observation;
  close: () => void;
  children?: React.ReactNode;
}) {
  return (
    <Modal label={t('Original observation')} close={close} wide>
      <h2>{t('Original observation')}</h2>
      <p>
        <strong>
          {hz(source.frequency_hz)} · {source.level ?? '—'} {t(source.level_unit)}
        </strong>{' '}
        · {source.mode}
      </p>
      <p className="mono break">
        {source.id}
        <br />
        {utc(source.interval.started_at)} → {utc(source.interval.ended_at)}
      </p>
      {children}
      <pre className="raw-observation">{originalObservationJSON(source)}</pre>
      <button onClick={close}>{t('Close original')}</button>
    </Modal>
  );
}
export function EventReceiverPanorama({
  api,
  source,
  context,
}: {
  api: Api;
  source: Observation;
  context: string;
}) {
  const pass = source.metadata?.scan_pass;
  const path =
    /^\d+$/.test(String(pass)) && /^[0-9a-f]{64}$/.test(context)
      ? '/sessions/' +
        encodePathSegment(source.session_id) +
        '/streams/' +
        encodePathSegment(source.stream_id) +
        '/receiver-panorama?' +
        new URLSearchParams({ pass: String(pass), context_id: context, columns: '512' })
      : undefined;
  const read = useArchiveRead<ReceiverPass>(api, path);
  if (!path) return null;
  return (
    <section aria-label={t('Receiver panorama')}>
      <h3>
        {t('Receiver panorama')} · {t('Pass {pass}', { pass: String(pass) })}
      </h3>
      {read?.value ? (
        <ReceiverPlot passes={[read.value]} />
      ) : read?.error ? (
        <p role="alert">{read.error}</p>
      ) : (
        <progress />
      )}
    </section>
  );
}
export async function readObservation(
  api: Api,
  session: string,
  stream: string,
  id: string,
  signal: AbortSignal,
) {
  const source = await api.get<Observation>(
    '/sessions/' + encodePathSegment(session) + '/observations/' + encodePathSegment(id),
    signal,
  );
  if (
    source.id !== id ||
    source.session_id !== session ||
    source.stream_id !== stream ||
    source.level_unit !== 's_meter_count'
  )
    throw Error('Source evidence mismatch');
  return source;
}

function ReceiverPlot({
  passes,
  waterfall = false,
  select,
}: {
  passes: ReceiverPass[];
  waterfall?: boolean;
  select?: (p: ReceiverPass, f: number) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null),
    p = passes[0];
  const max = Math.max(1, ...passes.map((row) => Math.max(0, ...row.bins.map((b) => b.level))));
  useEffect(() => {
    const el = canvas.current;
    if (!el || !p) return;
    function paint() {
      if (!el) return;
      const { width, height } = el.getBoundingClientRect(),
        dpr = Math.min(devicePixelRatio || 1, 2);
      el.width = Math.max(1, Math.round(width * dpr));
      el.height = Math.max(1, Math.round(height * dpr));
      const ctx = el.getContext('2d');
      if (!ctx || !width || !height) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const css = getComputedStyle(el),
        x = (freq: number) => ((freq - p.start_hz) / (p.stop_hz - p.start_hz)) * (width - 1);
      if (!waterfall) {
        ctx.strokeStyle = css.getPropertyValue('--line');
        ctx.beginPath();
        for (let i = 0; i <= 4; i++) {
          const y = 1 + ((height - 2) * i) / 4;
          ctx.moveTo(0, y);
          ctx.lineTo(width, y);
        }
        ctx.stroke();
      }
      const rowH = height / passes.length;
      passes.forEach((row, r) => {
        let lastEnd = -2;
        ctx.strokeStyle = css.getPropertyValue('--signal');
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        for (const b of row.bins) {
          const runs = measuredRuns(row, b);
          if (waterfall) {
            const strength = 1 - b.level / max;
            ctx.fillStyle =
              'hsl(' +
              Math.round(218 - 166 * strength) +
              ' 75% ' +
              Math.round(32 + 27 * strength) +
              '%)';
            for (const [lo, hi] of runs) {
              const left = x(frequencyAt(row, lo)),
                right = x(frequencyAt(row, hi));
              const gapLeft = lo ? (left - x(frequencyAt(row, lo - 1))) / 2 : 0;
              const gapRight =
                hi + 1 < row.total_points ? (x(frequencyAt(row, hi + 1)) - right) / 2 : 0;
              ctx.fillRect(
                left - gapLeft,
                r * rowH,
                Math.max(1, right - left + gapRight + gapLeft),
                Math.max(1, rowH - 1),
              );
            }
          } else {
            const at = x(b.frequency_hz),
              y = 2 + ((height - 4) * b.level) / max,
              full = b.measured_points === b.end_index - b.start_index + 1;
            if (full && lastEnd === b.start_index - 1) ctx.lineTo(at, y);
            else ctx.moveTo(at, y);
            ctx.fillStyle = css.getPropertyValue('--signal');
            ctx.fillRect(at - 1.5, y - 1.5, 3, 3);
            lastEnd = full ? b.end_index : -2;
          }
        }
        if (!waterfall) ctx.stroke();
      });
    }
    const size = new ResizeObserver(paint);
    size.observe(el);
    const theme = new MutationObserver(paint);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    paint();
    return () => {
      size.disconnect();
      theme.disconnect();
    };
  }, [passes, waterfall, max, p]);
  if (!p) return null;
  return (
    <>
      {!waterfall && (
        <small>
          {t('S-meter counts · lower is stronger')} · 0–{formatQuantity(max)}
        </small>
      )}
      <canvas
        className={waterfall ? 'receiver-waterfall' : 'receiver-trace'}
        ref={canvas}
        role="img"
        aria-label={t(waterfall ? 'Waterfall by pass' : 'Measured receiver spectrum')}
        onClick={(event) => {
          if (!select) return;
          const rect = event.currentTarget.getBoundingClientRect(),
            fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
          const row =
            passes[
              waterfall
                ? Math.min(
                    passes.length - 1,
                    Math.floor(((event.clientY - rect.top) / rect.height) * passes.length),
                  )
                : 0
            ];
          const target = p.start_hz + fraction * (p.stop_hz - p.start_hz);
          const index = Math.min(p.total_points - 1, Math.round((target - p.start_hz) / p.step_hz));
          const nearest = frequencyAt(p, index);
          select(
            row,
            Math.abs(p.stop_hz - target) < Math.abs(nearest - target) ? p.stop_hz : nearest,
          );
        }}
      />
      <div className="chart-caption">
        <span>{p.start_hz / 1e6} MHz</span>
        <span>{p.stop_hz / 1e6} MHz</span>
      </div>
    </>
  );
}

export function ReceiverPanorama({ desk }: { desk: Desk }) {
  const project = desk.project!;
  const sid = project.active_session_id,
    stream = project.primary_stream_id || project.receiver_stream_id!;
  const base =
    '/sessions/' +
    encodePathSegment(sid) +
    '/streams/' +
    encodePathSegment(stream) +
    '/receiver-panorama';
  const [chosen, setChosen] = useState<{ pass: number; context?: string }>(),
    [retry, setRetry] = useState(0);
  const [older, setOlder] = useState<ReceiverPass[]>([]),
    [pastError, setPastError] = useState('');
  const query = (pass?: number, context?: string) =>
    '?' +
    new URLSearchParams({
      columns: '512',
      ...(pass && { pass: String(pass) }),
      ...(context && { context_id: context }),
    });
  const available = desk.capabilities?.features.ardv1_receiver_panorama === true;
  const acquiring = desk.guard?.active || project.state === 'collecting';
  const reading = useArchiveRead<ReceiverPass>(
    desk.api,
    available ? base + query(chosen?.pass, chosen?.context) : undefined,
    acquiring && !chosen ? 3000 : 0,
    false,
    retry,
  );
  const current = reading?.value,
    latest = current?.pass;
  useEffect(() => {
    setOlder([]);
    setPastError('');
    if (!latest || latest <= 1) return;
    const controller = new AbortController();
    async function read() {
      const rows: ReceiverPass[] = [];
      for (let n = latest! - 1; n >= Math.max(1, latest! - 7); n--) {
        try {
          rows.push(
            await desk.api.get<ReceiverPass>(
              base + query(n, current?.context_id),
              controller.signal,
            ),
          );
        } catch (error) {
          if (!controller.signal.aborted) setPastError(message(error));
          break;
        }
        if (controller.signal.aborted) return;
      }
      if (!controller.signal.aborted) setOlder(rows);
    }
    void read();
    return () => controller.abort();
  }, [desk.api, base, latest, current?.context_id, retry]);
  const passes = useMemo(
    () =>
      current
        ? [
            current,
            ...older.filter(
              (p) =>
                p.pass < current.pass &&
                p.context_id === current.context_id &&
                p.start_hz === current.start_hz &&
                p.stop_hz === current.stop_hz &&
                p.step_hz === current.step_hz,
            ),
          ]
        : [],
    [current, older],
  );
  const trace = useMemo(() => (current ? [current] : []), [current]);
  const [source, setSource] = useState<Observation>(),
    [error, setError] = useState(''),
    [freq, setFreq] = useState('');
  const [busy, setBusy] = useState(false),
    controller = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => controller.current?.abort(), []);
  function reset() {
    controller.current?.abort();
    setError('');
    setBusy(false);
    setSource(undefined);
  }
  async function inspect(p: ReceiverPass, frequency: number, id?: string) {
    reset();
    const pending = new AbortController();
    controller.current = pending;
    setBusy(true);
    try {
      const obs = id
        ? await readObservation(desk.api, sid, stream, id, pending.signal)
        : await desk.api.get<Observation>(
            base +
              '/point?' +
              new URLSearchParams({
                pass: String(p.pass),
                frequency_hz: String(frequency),
                ...(p.context_id && { context_id: p.context_id }),
              }),
            pending.signal,
          );
      if (
        obs.session_id !== sid ||
        obs.stream_id !== stream ||
        Number(obs.frequency_hz) !== frequency ||
        obs.level_unit !== 's_meter_count'
      )
        throw Error('Source evidence mismatch');
      if (!pending.signal.aborted) setSource(obs);
    } catch (cause) {
      if (!pending.signal.aborted)
        setError(
          cause instanceof ApiError && cause.code === 'not_found'
            ? 'No original point at this frequency'
            : message(cause),
        );
    } finally {
      if (!pending.signal.aborted) setBusy(false);
    }
  }
  const strongest = current?.bins.reduce<ReceiverPass['bins'][number] | undefined>(
    (best, b) => (!best || b.level < best.level ? b : best),
    undefined,
  );
  return (
    <section className="panel receiver-panorama" aria-label={t('Receiver panorama')}>
      <div className="panel-heading">
        <div>
          <div className="eyebrow">AOR AR-DV1 · FM</div>
          <h2>{t('Receiver panorama')}</h2>
        </div>
        {current && (
          <span className="badge">{t(current.complete ? 'Complete pass' : 'Partial pass')}</span>
        )}
      </div>
      <div className="actions">
        <button
          disabled={!latest || latest < 2}
          onClick={() => {
            reset();
            setChosen({ pass: latest! - 1, context: current?.context_id });
          }}
        >
          {t('Previous pass')}
        </button>
        <button
          disabled={!chosen}
          onClick={() => {
            reset();
            setChosen(undefined);
          }}
        >
          {t('Follow latest pass')}
        </button>
        <button onClick={() => setRetry((n) => n + 1)}>{t('Refresh')}</button>
      </div>
      {current ? (
        <>
          <div className="spectrum-metrics">
            <div>
              <small>{t('Survey pass')}</small>
              <strong>{t('Pass {pass}', { pass: current.pass })}</strong>
            </div>
            <div>
              <small>{t('Measured frequencies')}</small>
              <strong>
                {formatQuantity(current.measured_points)} / {formatQuantity(current.total_points)}
              </strong>
            </div>
            <div>
              <small>{t('Age of last sample')}</small>
              <strong>
                <SourceAge at={current.ended_at} />
              </strong>
            </div>
          </div>
          <p className="muted">
            {t('{count} unmeasured', {
              count: formatQuantity(current.total_points - current.measured_points),
            })}{' '}
            · {hz(current.step_hz)} · {utc(current.started_at)} → {utc(current.ended_at)}
          </p>
          <ReceiverPlot passes={trace} select={(p, f) => void inspect(p, f)} />
          <h3>{t('Waterfall by pass')}</h3>
          <ReceiverPlot passes={passes} waterfall select={(p, f) => void inspect(p, f)} />
          <p className="muted">
            {t(
              'Newest pass first. Dark gaps have no archived reading. Colour shows the strongest measured count in each column.',
            )}{' '}
            {passes.map((p) => p.pass).join(' / ')}
          </p>
          <form
            className="actions"
            onSubmit={(e) => {
              e.preventDefault();
              void inspect(current, Number(freq));
            }}
          >
            <label>
              {t('Inspect frequency (Hz)')}
              <input
                required
                type="number"
                min={current.start_hz}
                max={current.stop_hz}
                step="1"
                placeholder={String(current.start_hz)}
                value={freq}
                onChange={(e) => setFreq(e.target.value)}
              />
            </label>
            <button disabled={busy || !freq}>{t('Open original point')}</button>
            <button
              type="button"
              disabled={busy || !strongest}
              onClick={() => {
                if (strongest)
                  void inspect(current, strongest.frequency_hz, strongest.observation_id);
              }}
            >
              {t('Strongest original')}
            </button>
          </form>
        </>
      ) : (
        <p className="empty">
          {t(
            available
              ? 'Waiting for the first archived receiver pass'
              : 'This node does not provide archived receiver panoramas.',
          )}
        </p>
      )}
      {(error || reading?.error || pastError) && (
        <p role="alert" className="error-text">
          {t(error || reading?.error || pastError)}
        </p>
      )}
      {source && <OriginalObservation source={source} close={() => setSource(undefined)} />}
    </section>
  );
}

export function ReceiverHold({ desk }: { desk: Desk }) {
  const [token, setToken] = useState(''),
    [frequency, setFrequency] = useState('');
  const remote = desk.guard?.manual.find((l) => l.role === 'receiver');
  const busy = desk.busy || !desk.guard?.active;
  const n = Number(frequency),
    valid = Number.isSafeInteger(n) && n >= 100000 && n <= 1300000000 && n % 10 === 0;
  const project = desk.project!;
  return (
    <div className="receiver-hold">
      <h3>{t('Check one frequency')}</h3>
      <p className="muted">
        {t(
          'The same receiver pauses the wide scan while tuned to this frequency. Resume explicitly to return to the cyclic plan.',
        )}
      </p>
      {remote && (
        <p className="callout">
          {t(
            remote.returning
              ? 'Restoring the cyclic plan'
              : remote.expired
                ? 'Receiver lease expired · explicit return required'
                : 'Broad coverage paused',
          )}
        </p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!valid || busy) return;
          void desk.run(
            'Hold frequency and pause panorama',
            async () => {
              let lease = token;
              if (!lease) {
                const result = await desk.api.project<{ token: string }>(
                  project.id,
                  'manual/acquire',
                  { role: 'receiver' },
                );
                lease = result.token;
                setToken(lease);
              }
              await desk.api.project(project.id, 'manual/tune', {
                token: lease,
                frequency_hz: n,
                mode: 'FM',
              });
            },
            ['status'],
          );
        }}
      >
        <label>
          {t('Hold frequency (Hz)')}
          <input
            type="number"
            required
            min={100000}
            max={1300000000}
            step={10}
            value={frequency}
            onChange={(e) => setFrequency(e.target.value)}
          />
        </label>
        <div className="actions">
          <button
            disabled={
              busy || !valid || (!!remote && (!token || remote.expired || remote.returning))
            }
          >
            {t('Hold frequency and pause panorama')}
          </button>
          <button
            type="button"
            disabled={desk.busy || !token || remote?.returning}
            onClick={async () => {
              const result = await desk.run(
                'Resume broad coverage',
                () => desk.api.project(project.id, 'manual/return', { token }),
                ['status'],
              );
              if (result) setToken('');
            }}
          >
            {t('Resume broad coverage')}
          </button>
        </div>
      </form>
    </div>
  );
}
