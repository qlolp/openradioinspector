import { useEffect, useRef, useState } from 'react';
import type { Api } from './api';
import { hz, utc } from './components';
import { getLocale, t } from './i18n';
import {
  RfOriginalPlayback,
  readRfOriginal,
  useArchiveRead,
  type OpenRfOriginal,
} from './RfExplorerOriginals';
import { RfPassPlot, missingRanges, strongest } from './RfPassPlot';
import { RfPassMarkers } from './RfPassMarkers';
import type { PassDetail, PassPoint, PassPage, MarkerReading } from './rf-pass';
import { message } from './useDesk';
import { encodePathSegment } from './wire';

const number = (n: number) => n.toLocaleString(getLocale(), { maximumFractionDigits: 6 });
const seconds = (n: number | null) => (n == null ? '—' : t('{value} s', { value: number(n) }));
export function SourceAge({ at }: { at?: string }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!at) return <>—</>;
  const delta = (now - Date.parse(at)) / 1000;
  if (!Number.isFinite(delta) || delta < 0) return <>—</>;
  if (delta < 60) return <>{seconds(Math.floor(delta))}</>;
  if (delta < 3600) return <>{t('{value} min', { value: Math.floor(delta / 60) })}</>;
  if (delta < 86400) return <>{t('{value} h', { value: Math.floor(delta / 3600) })}</>;
  return <>{t('{value} d', { value: Math.floor(delta / 86400) })}</>;
}
export interface LocalHold {
  since: string;
  visible: boolean;
}
const archiveEpoch = '1970-01-01T00:00:00.000Z';
type Context = {
  hold: LocalHold;
  setHold: (next: LocalHold) => void;
  api: Api;
  session: string;
  stream: string;
  running: boolean;
  paused: boolean;
  bookmarks: boolean;
  readOnly: boolean;
  openOriginal: OpenRfOriginal;
};

const passComplete = (data: PassDetail) => data.summary.complete;

function PassEvidence({
  api,
  session,
  stream,
  running,
  paused,
  bookmarks,
  readOnly,
  openOriginal,
  passId,
  base,
  hold,
  setHold,
  listedStart,
  pin,
}: Context & {
  passId: string;
  base: string;
  listedStart?: string;
  pin: () => void;
}) {
  const { since: holdSince, visible: showHold } = hold;
  const path = base + '/' + encodePathSegment(passId);
  const query = new URLSearchParams({
    columns: '512',
    ...(showHold && holdSince && { hold_since: holdSince }),
  });
  const [revision, setRevision] = useState(0);
  const request = useArchiveRead<PassDetail>(
    api,
    path + '?' + query,
    running ? 5000 : 0,
    paused,
    revision,
    passComplete,
  );
  const data = request?.value,
    s = data?.summary;
  const [point, setPoint] = useState<PassPoint | null>(null),
    [held, setHeld] = useState(false);
  const [marker, setMarker] = useState<number>();
  const [note, setNote] = useState(''),
    [review, setReview] = useState(false);
  const [sourceError, setSourceError] = useState(''),
    [reading, setReading] = useState(false);
  const sourceRead = useRef<AbortController | null>(null);
  useEffect(() => () => sourceRead.current?.abort(), []);
  function select(p: PassPoint | null) {
    sourceRead.current?.abort();
    setReading(false);
    setSourceError('');
    setPoint(p);
    setMarker(undefined);
    setNote(p ? '' : 'No original point at this frequency');
  }
  const summaryStart = useRef<string | undefined>(listedStart);
  if (s) summaryStart.current = s.started_at;
  function changeHold(next: LocalHold) {
    select(null);
    setHeld(false);
    setNote('');
    setHold(next);
  }
  function resetHold() {
    const since = running ? new Date().toISOString() : summaryStart.current;
    if (since) changeHold({ since, visible: true });
  }
  const resetLabel = t(
    running ? 'Reset software Max Hold' : 'Reset Max Hold to start of this pass',
  );
  const toggleHold = () => changeHold({ ...hold, visible: !showHold });
  const frequency = marker ?? point?.frequency_hz;
  const onGrid =
    s &&
    frequency != null &&
    frequency >= s.start_hz &&
    frequency <= s.stop_hz &&
    (frequency - s.start_hz) % s.step_hz === 0;
  const exactQuery = onGrid
    ? path +
      '/point?' +
      new URLSearchParams({
        frequency_hz: String(frequency),
        ...(held && showHold && holdSince && { hold_since: holdSince }),
      })
    : undefined;
  const probe = useArchiveRead<MarkerReading>(
    api,
    exactQuery,
    running && !s?.complete ? 5000 : 0,
    paused,
  );
  const exact = held ? probe?.value?.held : probe?.value?.current;
  const columns = held ? (data?.hold_columns ?? []) : (data?.columns ?? []);
  const column =
    frequency == null ? null : (columns.find((p) => p?.frequency_hz === frequency) ?? null);
  // On an older panorama service only an exact measured bucket can stand in for lookup.
  const unavailable =
    probe?.code === 'not_found' ||
    probe?.code === 'not_implemented' ||
    probe?.error === 'Core HTTP 404';
  const markerPoint = exact ?? (unavailable ? column : null);
  useEffect(() => {
    if (marker !== undefined && !point && markerPoint) setPoint(markerPoint);
  }, [marker, markerPoint, point]);
  const latestReading =
    exact?.last_measured_at ?? column?.last_measured_at ?? point?.last_measured_at;
  async function openPoint(p: PassPoint) {
    sourceRead.current?.abort();
    const controller = new AbortController();
    sourceRead.current = controller;
    setReading(true);
    setSourceError('');
    try {
      const sw = await readRfOriginal(api, session, stream, p.sweep_id, controller.signal);
      if (controller.signal.aborted) return;
      if (
        Number(sw.frequencies_hz[p.point_index]) !== p.frequency_hz ||
        sw.levels[p.point_index] !== p.level_dbm ||
        sw.interval.started_at !== p.started_at ||
        sw.interval.ended_at !== p.ended_at
      )
        throw Error('Source evidence mismatch');
      openOriginal(sw, p.point_index);
    } catch (cause) {
      if (!controller.signal.aborted) setSourceError(message(cause));
    } finally {
      if (!controller.signal.aborted) setReading(false);
    }
  }
  if (!data || !s)
    return (
      <>
        <p className={request?.error ? 'error-text' : 'muted'} role="status">
          {t(request?.error || 'Loading pass panorama…')}{' '}
          {request?.error && (
            <button onClick={() => setRevision((r) => r + 1)}>{t('Retry')}</button>
          )}
        </p>
        {request?.error && showHold && holdSince && (
          <div className="actions">
            <button onClick={resetHold}>{resetLabel}</button>
            <button onClick={toggleHold}>{t('Hide software Max Hold')}</button>
          </div>
        )}
      </>
    );
  const gaps = missingRanges(s, data.coverage, s.step_hz);
  const lastMeasured = latestReading;
  return (
    <>
      {request.error && (
        <p className="error-text" role="alert">
          {t(request.error)} <button onClick={() => setRevision((r) => r + 1)}>{t('Retry')}</button>
        </p>
      )}
      <div className="rf-pass-heading">
        <h3>{t('Pass {pass}', { pass: s.pass })}</h3>
        <span className="badge">{t(s.complete ? 'Complete' : 'Partial pass')}</span>
        {s.simulated && <span className="badge">{t('Simulated')}</span>}
      </div>
      <p className="muted">
        {hz(s.start_hz)} – {hz(s.stop_hz)} · {t('Bin spacing')} {hz(s.step_hz)}
      </p>
      <progress
        className="rf-progress"
        aria-label={t('Unique frequency coverage')}
        value={s.unique_points}
        max={s.expected_points}
      />
      <dl className="rf-pass-metrics">
        <div>
          <dt>{t('Windows received')}</dt>
          <dd>
            {number(s.received_tiles)} / {number(s.tiles)}
          </dd>
        </div>
        <div>
          <dt>{t('Unique frequencies')}</dt>
          <dd>
            {number(s.unique_points)} / {number(s.expected_points)}
          </dd>
        </div>
        <div>
          <dt>{t('Pass duration')}</dt>
          <dd>{seconds(s.duration_s)}</dd>
        </div>
        <div>
          <dt>{t('Measured revisit')}</dt>
          <dd>{seconds(s.revisit_s)}</dd>
        </div>
      </dl>
      <RfPassPlot data={data} held={held} selected={point} marker={marker} choose={select} />
      <div className="spectrum-legend">
        <span>
          <i className="legend-source" />
          {t('Selected pass')}
        </span>
        {showHold && holdSince && (
          <span>
            <i className="rf-hold-key" />
            {t('Software Max Hold')}
          </span>
        )}
      </div>
      <details className="rf-gaps">
        <summary>{t('Unmeasured gaps')}</summary>
        {gaps.length ? (
          <ul>
            {gaps.map((g) => (
              <li key={g.start_hz}>
                {hz(g.start_hz)} – {hz(g.stop_hz)}
              </li>
            ))}
          </ul>
        ) : (
          <p>{t('All grid frequencies measured')}</p>
        )}
      </details>
      <div className="actions">
        <button disabled={!columns.some(Boolean)} onClick={() => select(strongest(columns))}>
          {t('Select strongest point')}
        </button>
        {holdSince !== archiveEpoch && (
          <button onClick={() => changeHold({ since: archiveEpoch, visible: true })}>
            {t('Enable archive Max Hold')}
          </button>
        )}
        <button
          onClick={() => {
            setPoint(null);
            setNote('');
            setHeld(false);
            resetHold();
          }}
        >
          {resetLabel}
        </button>
        {holdSince && (
          <button
            onClick={() => {
              setPoint(null);
              setHeld(false);
              toggleHold();
            }}
          >
            {t(showHold ? 'Hide software Max Hold' : 'Show software Max Hold')}
          </button>
        )}
      </div>
      {showHold && holdSince && (
        <>
          <p className="muted">
            {t(
              holdSince === archiveEpoch
                ? 'Software Max Hold across compatible archived passes'
                : 'Software Max Hold from',
            )}{' '}
            · {utc(holdSince)}
          </p>
          <label>
            {t('Inspect trace')}
            <select
              value={held ? 'hold' : 'pass'}
              onChange={(e) => {
                setHeld(e.target.value === 'hold');
                select(null);
                setNote('');
              }}
            >
              <option value="pass">{t('Selected pass')}</option>
              <option value="hold">{t('Software Max Hold')}</option>
            </select>
          </label>
        </>
      )}
      {marker !== undefined && !point && (
        <p className="muted" role="status">
          {hz(marker)} ·{' '}
          {t(
            !onGrid
              ? 'Outside the pass grid'
              : probe?.error && !unavailable
                ? probe.error
                : !probe
                  ? 'Loading original frequency…'
                  : 'No original point at this frequency',
          )}
        </p>
      )}
      {note && (
        <p className="muted" role="status">
          {t(note)}
          {marker !== undefined && <> · {hz(marker)}</>}
        </p>
      )}
      {point && (
        <div className="rf-evidence">
          <strong>
            {hz(point.frequency_hz)} · {number(point.level_dbm)} dBm
          </strong>
          <p className="mono break">
            {point.sweep_id} · {t('Source index')} {point.point_index} ·{' '}
            {t('Window {tile}', { tile: point.tile })}
          </p>
          <p>
            {utc(point.started_at)} → {utc(point.ended_at)} · {t('Source age')}{' '}
            <SourceAge at={point.ended_at} />
          </p>
          <p>
            {t('Selected frequency last measured')} · {utc(lastMeasured ?? undefined)} ·{' '}
            <SourceAge at={lastMeasured ?? undefined} />
          </p>
          <button disabled={reading} onClick={() => void openPoint(point)}>
            {t('Open original sweep')}
          </button>
        </div>
      )}
      {sourceError && (
        <p className="error-text" role="alert">
          {t(sourceError)}
        </p>
      )}
      {bookmarks && (
        <RfPassMarkers
          api={api}
          session={session}
          point={point}
          readOnly={readOnly}
          select={(f) => {
            select(null);
            setNote('');
            setMarker(f);
          }}
        />
      )}
      <dl className="status-list">
        <div>
          <dt>{t('Reported input')}</dt>
          <dd>
            {t(
              s.active_input === 'right'
                ? 'Right SMA'
                : s.active_input === 'left'
                  ? 'Left SMA'
                  : s.active_input,
            )}{' '}
            ·{' '}
            {s.active_board === 'expansion'
              ? 'WSUB3G'
              : s.active_board === 'main'
                ? 'WSUB1G'
                : s.active_board}
          </dd>
        </div>
        <div>
          <dt>{t('Reported detector')}</dt>
          <dd>{s.detector}</dd>
        </div>
        <div>
          <dt>{t('Reported RBW')}</dt>
          <dd>{hz(s.rbw_hz)}</dd>
        </div>
        <div>
          <dt>{t('Reported amplitude offset')}</dt>
          <dd>{number(s.offset_db)} dB</dd>
        </div>
        <div>
          <dt>{t('Source age')}</dt>
          <dd>
            <SourceAge at={s.ended_at} />
          </dd>
        </div>
        <div>
          <dt>{t('Acquisition interval')}</dt>
          <dd>
            {utc(s.started_at)} → {utc(s.ended_at)}
          </dd>
        </div>
        <div>
          <dt>{t('Pass ID')}</dt>
          <dd className="mono break">{s.pass_id}</dd>
        </div>
      </dl>
      <button
        aria-expanded={review}
        onClick={() => {
          pin();
          setReview((v) => !v);
        }}
      >
        {t('Review original windows')}
      </button>
      {review && (
        <RfOriginalPlayback
          api={api}
          session={session}
          stream={stream}
          paused={paused}
          passPath={path}
          openOriginal={openOriginal}
        />
      )}
    </>
  );
}

export function RfExplorerPass(props: Context) {
  const { api, session, stream, running, paused } = props;
  const base =
    '/sessions/' +
    encodePathSegment(session) +
    '/streams/' +
    encodePathSegment(stream) +
    '/rf-explorer/passes';
  const [pages, setPages] = useState<string[]>([]),
    [selected, setSelected] = useState('');
  const [revision, setRevision] = useState(0);
  const before = pages.at(-1);
  const path = base + '?' + new URLSearchParams({ limit: '12', ...(before && { before }) });
  const page = useArchiveRead<PassPage>(
    api,
    path,
    running && !before && !selected ? 5000 : 0,
    paused,
    revision,
  );
  const passId = selected || page?.value?.items[0]?.pass_id;
  return (
    <section className="rf-pass" aria-label={t('Survey pass panorama')}>
      <div className="rf-pass-select">
        <label>
          {t('Survey pass')}
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">{t(before ? 'First pass on this page' : 'Follow latest pass')}</option>
            {selected && !page?.value?.items.some((s) => s.pass_id === selected) && (
              <option value={selected}>{selected}</option>
            )}
            {page?.value?.items.map((s) => (
              <option key={s.pass_id} value={s.pass_id}>
                {t('Pass {pass}', { pass: s.pass })} · {utc(s.started_at)}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button
            disabled={!before}
            onClick={() => {
              setSelected('');
              setPages((p) => p.slice(0, -1));
            }}
          >
            {t('Newer passes')}
          </button>
          <button
            disabled={!page?.value?.next_before}
            onClick={() => {
              setSelected('');
              setPages((p) => [...p, page!.value!.next_before!]);
            }}
          >
            {t('Older passes')}
          </button>
        </div>
      </div>
      {page?.error && (
        <p className="error-text" role="alert">
          {t(page.error)} <button onClick={() => setRevision((r) => r + 1)}>{t('Retry')}</button>
        </p>
      )}
      {passId ? (
        <PassEvidence
          key={passId}
          {...props}
          running={running && !selected && !before && page?.value?.items[0]?.pass_id === passId}
          passId={passId}
          base={base}
          listedStart={page?.value?.items.find((p) => p.pass_id === passId)?.started_at}
          pin={() => setSelected(passId)}
        />
      ) : (
        <p className="empty">
          {t(page?.value ? 'No archived survey passes' : 'Loading survey passes…')}
        </p>
      )}
    </section>
  );
}
