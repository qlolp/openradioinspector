import { useEffect, useRef, useState } from 'react';
import type { Api } from './api';
import { encodePathSegment, originalObservationJSON } from './wire';
import { Panel, hz, titleCase, utc } from './components';
import { t } from './i18n';
import { formatBytes, formatQuantity, measuredRate, recoveryNotices } from './station';
import { PANORAMA_POINTS, panoramaColumns, type BandPanorama } from './panorama';
import type { FrequencyRange } from './spectrum-view';
import type { BandCoverage, MonitoringEvent, Observation, WorkerStatus } from './types';
import type { Desk } from './useDesk';

export function EventQueue({
  desk,
  selected,
  open,
}: {
  desk: Desk;
  selected?: string;
  open: (event: MonitoringEvent) => void;
}) {
  return (
    <Panel
      title={t('Detected events')}
      className="event-queue"
      aside={
        <button
          disabled={desk.busy || !desk.project}
          onClick={() =>
            void desk.run('Refresh detected events', () => desk.refresh(['events']), [])
          }
        >
          {t('Refresh events')}
        </button>
      }
    >
      <div className="queue-count">
        {desk.events.length} {t('loaded')}
      </div>
      {desk.events.length ? (
        <div className="queue-list" aria-label={t('Detected events')}>
          {desk.events.map((item, index) => (
            <div
              key={item.id}
              data-testid="event-row"
              data-event-id={item.id}
              className="queue-row"
              data-selected={item.id === selected}
            >
              <div id={'queue-source-' + index}>
                <strong className="mono">{hz(item.center_frequency_hz)}</strong>
                <span>
                  {titleCase(item.kind)} · {titleCase(item.analysis?.state || 'pending')}
                </span>
                <small className="mono">
                  {utc(item.last_seen)} · {formatQuantity(item.peak_level)} {t('dBm')}
                </small>
              </div>
              <button
                aria-haspopup="dialog"
                aria-describedby={'queue-source-' + index}
                disabled={desk.busy}
                onClick={() => open(item)}
              >
                {t('Details')}
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="empty">
          {t('No detected events. Collection and detection belong to the core.')}
        </p>
      )}
    </Panel>
  );
}

export function EventReceiver({ event, api }: { event: MonitoringEvent; api: Api }) {
  const linked = event.analysis?.receiver_status;
  const id = event.analysis?.observation_ids?.at(-1);
  const sid = event.run_session_id ?? event.source_refs?.at(-1)?.session_id;
  const [fetched, setFetched] = useState<Observation>();
  const [error, setError] = useState('');
  useEffect(() => {
    setFetched(undefined);
    setError('');
    if (linked?.length || !sid || !id) return;
    const controller = new AbortController();
    void api
      .get<Observation>(
        '/sessions/' + encodePathSegment(sid) + '/observations/' + encodePathSegment(id),
        controller.signal,
      )
      .then((row) => {
        if (controller.signal.aborted) return;
        if (row.id !== id || row.session_id !== sid) throw Error('Source evidence mismatch');
        setFetched(row);
      })
      .catch((reason) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : 'Unavailable');
      });
    return () => controller.abort();
  }, [api, event.id, sid, id, linked?.length]);
  const rows = linked?.length
    ? linked
    : fetched?.id === id && fetched?.session_id === sid && fetched
      ? [fetched]
      : [];
  return (
    <section className="event-receiver" aria-label={t('Event receiver observations')}>
      <h3>{t('Event receiver observations')}</h3>
      {rows.length ? (
        <ul>
          {rows.map((row) => (
            <li key={row.id}>
              <strong>
                {hz(row.frequency_hz)} · {row.mode} · {row.level ?? '—'} {t(row.level_unit)}
              </strong>
              <small>
                {t('Frequency source')} ·{' '}
                {typeof row.metadata?.frequency_source === 'string'
                  ? titleCase(row.metadata.frequency_source)
                  : t('Unavailable')}
              </small>
              <small>
                {utc(row.interval.started_at)} → {utc(row.interval.ended_at)}
              </small>
              <code>{row.id}</code>
              <details>
                <summary>{t('Original observation')}</summary>
                <pre>{originalObservationJSON(row)}</pre>
              </details>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">
          {t(
            id ? 'Receiver observation unavailable' : 'No receiver reading attached to this event',
          )}
        </p>
      )}
      {id && !rows.length && <p className="mono break">{id}</p>}
      {(error || event.analysis?.error) && (
        <p className="error-text">{t(error || event.analysis!.error!)}</p>
      )}
    </section>
  );
}

export function Recovery({ desk }: { desk: Desk }) {
  const notices = recoveryNotices(desk.guard, {
    ...desk.storage,
    write_probe: desk.preflight?.storage?.write_probe ?? desk.storage?.write_probe,
  });
  if (!notices.length) return null;
  return (
    <section className="recovery-strip" aria-label={t('Recovery')} role="status">
      <div>
        {notices.map((item) => (
          <p key={item.kind}>
            <strong>{t(item.title)}</strong> {t(item.detail)} {item.error}
          </p>
        ))}
      </div>
      <button
        disabled={desk.busy}
        onClick={() =>
          void desk.run(
            'Recheck status',
            () => desk.refresh(['status', 'preflight', 'storage']),
            [],
          )
        }
      >
        {t('Recheck status')}
      </button>
    </section>
  );
}

export function StationStatus({ desk, analyzer }: { desk: Desk; analyzer?: WorkerStatus }) {
  const storage = desk.storage,
    rate = measuredRate(analyzer);
  const missing = t('Unavailable');
  return (
    <Panel title={t('Node & evidence')} className="station-status">
      <dl className="status-list">
        <div>
          <dt>{t('Analyzer worker')}</dt>
          <dd>
            {analyzer
              ? [titleCase(analyzer.state), analyzer.adapter].filter(Boolean).join(' · ')
              : missing}
          </dd>
        </div>
        <div>
          <dt>{t('Committed sweep rate')}</dt>
          <dd>{rate == null ? missing : formatQuantity(rate, 3) + ' ' + t('sweeps/s')}</dd>
        </div>
        <div>
          <dt>{t('Writer operations in flight')}</dt>
          <dd>
            {storage?.writer_queue
              ? storage.writer_queue.queued_operations +
                ' / ' +
                storage.writer_queue.writer_capacity
              : missing}
          </dd>
        </div>
        <div>
          <dt>{t('Free on archive disk')}</dt>
          <dd>{formatBytes(storage?.filesystem_free_bytes)}</dd>
        </div>
        <div>
          <dt>{t('Daily archive forecast')}</dt>
          <dd>{formatBytes(storage?.projected_bytes_per_day)}</dd>
        </div>
        <div>
          <dt>{t('Estimated retention')}</dt>
          <dd>
            {storage?.estimated_retention_seconds == null
              ? missing
              : formatQuantity(storage.estimated_retention_seconds / 3600, 2) + ' ' + t('hours')}
          </dd>
        </div>
      </dl>
      <details>
        <summary>{t('Storage and measurement details')}</summary>
        <dl className="status-list">
          <div>
            <dt>{t('Writer bytes in flight')}</dt>
            <dd>
              {storage?.writer_queue
                ? formatBytes(storage.writer_queue.retained_bytes) +
                  ' / ' +
                  formatBytes(storage.writer_queue.writer_byte_limit)
                : missing}
            </dd>
          </div>
          <div>
            <dt>{t('Mean stored raw sweep')}</dt>
            <dd>{formatBytes(storage?.mean_stored_sweep_bytes)}</dd>
          </div>
          <div>
            <dt>{t('Write-ahead log')}</dt>
            <dd>{formatBytes(storage?.wal_bytes)}</dd>
          </div>
          <div>
            <dt>{t('Backup reserve')}</dt>
            <dd>{formatBytes(storage?.backup_reserve_bytes)}</dd>
          </div>
          <div>
            <dt>{t('Available after reserves')}</dt>
            <dd>{formatBytes(storage?.available_bytes)}</dd>
          </div>
          <div>
            <dt>{t('Archive')}</dt>
            <dd>{desk.capabilities?.session_storage || missing}</dd>
          </div>
          <div>
            <dt>{t('Hardware validation')}</dt>
            <dd>
              {desk.guard?.hardware_validation
                ? titleCase(desk.guard.hardware_validation)
                : missing}
            </dd>
          </div>
        </dl>
        <p className="muted">
          {t(
            'Sweep rate uses the worker count and elapsed acquisition time. The daily forecast includes the core’s storage allowance.',
          )}
        </p>
        <button
          disabled={!desk.project || desk.busy}
          onClick={() => void desk.run('Refresh storage', () => desk.refresh(['storage']), [])}
        >
          {t('Refresh storage')}
        </button>
      </details>
    </Panel>
  );
}

const qualificationLabels = {
  supported: 'Supported by the receiver profile',
  unverified: 'Unverified frequency range',
  partial: 'Partially verified frequency range',
  synthetic: 'Training data',
};
export function Qualification({ label, value }: { label: string; value?: unknown }) {
  const receipt =
    value && typeof value === 'object' ? (value as Record<string, unknown>) : undefined;
  const status = receipt?.status;
  const known = typeof status === 'string' && Object.hasOwn(qualificationLabels, status);
  const limit = receipt?.verified_through_hz;
  return (
    <span className="measurement-qualification">
      {t(label)} ·{' '}
      <strong>
        {t(
          known
            ? qualificationLabels[status as keyof typeof qualificationLabels]
            : 'Not reported by the core',
        )}
      </strong>
      {known && typeof limit === 'number' && Number.isSafeInteger(limit) && limit > 0 && (
        <> · {t('Profile verified through {frequency}', { frequency: hz(limit) })}</>
      )}
      {known && receipt?.reason === 'reference_required_above_800_mhz' && (
        <> · {t('Above 800 MHz a reference measurement is required.')}</>
      )}
    </span>
  );
}

export function CoverageRow({ band }: { band: BandCoverage }) {
  return (
    <div className="coverage-row">
      <strong>
        {band.name} · {titleCase(band.state)}
      </strong>
      <span>
        {t('Age')} {band.age_seconds == null ? '—' : formatQuantity(band.age_seconds)} {t('s')} ·{' '}
        {t('Revisit')}{' '}
        {band.last_revisit_seconds == null ? '—' : formatQuantity(band.last_revisit_seconds)}{' '}
        {t('s')}
      </span>
      {band.window_count != null && (
        <span>
          {t('Observed windows')} {band.observed_windows ?? '—'} / {band.window_count} ·{' '}
          {t('Completed passes')} {band.completed_passes ?? '—'}
        </span>
      )}
      <Qualification label="Band qualification" value={band.measurement_qualification} />
      {band.last_window && (
        <small className="break">
          {hz(band.last_window.start_hz)} – {hz(band.last_window.stop_hz)} ·{' '}
          {utc(band.last_window.ended_at)} · {band.last_window.measurement_id}
          <br />
          <Qualification
            label="Latest window qualification"
            value={band.last_window.measurement_qualification}
          />
        </small>
      )}
      {band.deadline_missed && <strong className="warning-text">{t('Revisit overdue')}</strong>}
    </div>
  );
}

export function BandOverview({
  band,
  range,
  openOriginal,
  busy,
}: {
  band?: BandPanorama;
  range: FrequencyRange;
  openOriginal: (id: string) => void;
  busy: boolean;
}) {
  const [inspect, setInspect] = useState('');
  const currentIndex = inspect ? Number(inspect) - 1 : band?.lastIndex;
  const source = currentIndex == null ? undefined : band?.windows.get(currentIndex);
  const age = source ? (Date.now() - Date.parse(source.endedAt)) / 1000 : NaN;
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(1);
  useEffect(() => {
    const el = canvas.current!;
    const resize = () =>
      setWidth(Math.min(PANORAMA_POINTS, Math.max(1, Math.round(el.clientWidth))));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const el = canvas.current!,
      context = el.getContext('2d');
    if (!context) return;
    const height = el.height;
    context.clearRect(0, 0, width, height);
    context.fillStyle = '#4ac7af';
    const peaks = panoramaColumns(band, range, width);
    for (let x = 0; x < width; x++) {
      if (!Number.isFinite(peaks[x])) continue;
      const bar = Math.max(1, Math.min(height, ((peaks[x] + 130) * height) / 140));
      context.fillRect(x, height - bar, 1, bar);
    }
  }, [band, range.low, range.high, width]);
  return (
    <div className="band-overview">
      <strong>{t('Band panorama')}</strong>
      <span>
        {hz(range.low)} – {hz(range.high)}
      </span>
      <canvas
        ref={canvas}
        width={width}
        height={96}
        role="img"
        aria-label={t('Peak levels across retained windows. Gaps have no retained samples.')}
      />
      <small>
        {t('Latest window peaks on this client')} · {band?.windows.size ?? 0} / {band?.count ?? '—'}{' '}
        · −130 … +10 {t('dBm')}
      </small>
      {band && (
        <details className="panorama-inspector">
          <summary>{t('Inspect panorama window')}</summary>
          <label>
            {t('Window number')}
            <input
              type="number"
              min={1}
              max={band.count}
              step={1}
              placeholder={String(band.lastIndex + 1)}
              value={inspect}
              onChange={(e) => setInspect(e.target.value)}
            />
          </label>
          {source ? (
            <>
              <p className="mono">
                {hz(source.low)} – {hz(source.high)} · {utc(source.endedAt)}
              </p>
              <p className="mono break">
                {source.id} · {t('Age on this client')}{' '}
                {Number.isFinite(age) && age >= 0
                  ? formatQuantity(age, 0) + ' ' + t('s')
                  : t('Unavailable')}
              </p>
              <button disabled={busy} onClick={() => openOriginal(source.id)}>
                {t('Open window source')}
              </button>
            </>
          ) : (
            <p>{t('This client has no measurement for this window')}</p>
          )}
        </details>
      )}
    </div>
  );
}
