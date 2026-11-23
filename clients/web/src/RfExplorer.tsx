import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Spectrum, Waterfall } from './charts';
import { hz, Panel, titleCase, utc } from './components';
import { t } from './i18n';
import { RfExplorerPass, SourceAge, type LocalHold } from './RfExplorerPass';
import { readRfOriginal, type OpenRfOriginal } from './RfExplorerOriginals';
import { RfExplorerNewSession, RfPlanPreview, ReportedBoard } from './RfExplorerSession';
import { rfExplorerReadback, type RfExplorerReadback } from './rf-explorer';
import { type Desk, message } from './useDesk';
import type { DisplaySweep, Session, Sweep, WorkerStatus } from './types';
import { encodePathSegment } from './wire';

export function RfExplorerSettings({
  readback,
  frequencies,
  showPassId = false,
}: {
  readback?: RfExplorerReadback;
  frequencies: ArrayLike<number | bigint>;
  showPassId?: boolean;
}) {
  const survey = readback?.survey;
  return (
    <dl className="status-list">
      {readback?.simulated && (
        <div>
          <dt>{t('Source')}</dt>
          <dd>{t('Simulated')}</dd>
        </div>
      )}
      {survey && (
        <>
          <div>
            <dt>{t('10 kHz survey')}</dt>
            <dd>{t('Pass {pass} · segment {tile} of {tiles}', survey)}</dd>
          </div>
          <div>
            <dt>{t('Planned survey range')}</dt>
            <dd>
              {hz(survey.start_hz)} – {hz(survey.stop_hz)}
            </dd>
          </div>
          <div>
            <dt>{t('Measured segment')}</dt>
            <dd>
              {hz(frequencies[0])} – {hz(frequencies[frequencies.length - 1])}
            </dd>
          </div>
          {showPassId && (
            <div>
              <dt>{t('Pass ID')}</dt>
              <dd className="mono break">{survey.pass_id}</dd>
            </div>
          )}
        </>
      )}
      {readback?.active_board && (
        <div>
          <dt>{t('Reported input')}</dt>
          <dd>
            <ReportedBoard readback={readback} />
          </dd>
        </div>
      )}
      {readback?.board_min_hz && readback.board_max_hz && (
        <div>
          <dt>{t('Reported board limits')}</dt>
          <dd>
            {hz(readback.board_min_hz)} – {hz(readback.board_max_hz)}
          </dd>
        </div>
      )}
      {readback?.offset_db != null && (
        <div>
          <dt>{t(readback?.confirmed ? 'Reported amplitude offset' : 'Saved amplitude offset')}</dt>
          <dd>{readback.offset_db} dB</dd>
        </div>
      )}
      <div>
        <dt>{t(readback?.confirmed ? 'Reported detector' : 'Saved detector')}</dt>
        <dd>{readback?.detector ?? t('Unavailable')}</dd>
      </div>
      <div>
        <dt>{t(readback?.confirmed ? 'Reported RBW' : 'Saved RBW')}</dt>
        <dd>{hz(readback?.rbw_hz)}</dd>
      </div>
      <div>
        <dt>{t('Bin spacing')}</dt>
        <dd>{hz(readback?.step_hz)}</dd>
      </div>
    </dl>
  );
}

function Acquisition({
  desk,
  session,
  readOnly,
  openOriginal,
}: {
  desk: Desk;
  session: Session;
  readOnly: boolean;
  openOriginal: OpenRfOriginal;
}) {
  const [stream] = session.streams;
  const canAcquire = !readOnly && desk.capabilities?.features.rf_explorer_3g_spectrum === true;
  const hasPanorama = desk.capabilities?.features.rf_explorer_survey_panorama === true;
  const surveyUnsupported =
    stream.settings?.survey_step_hz === 10_000 &&
    desk.capabilities?.features.rf_explorer_10khz_survey !== true;
  const path = '/sessions/' + encodePathSegment(session.id);
  const [worker, setWorker] = useState<WorkerStatus>();
  const [workerError, setWorkerError] = useState('');
  const [revision, setRevision] = useState(0);
  const [history, setHistory] = useState<Sweep[]>();
  const [hold, setHold] = useState<LocalHold>({ since: '', visible: false });
  const [view, setView] = useState<'window' | 'pass'>('window');
  const [frozen, setFrozen] = useState<Pick<Desk['live'], 'rows' | 'presentation'>>();
  const [cursor, setCursor] = useState<{ row: DisplaySweep; index: number }>();
  const [sourceError, setSourceError] = useState('');
  const sourceRead = useRef<AbortController | null>(null);
  useEffect(() => () => sourceRead.current?.abort(), []);
  const historyRead = useRef<AbortController | null>(null);
  useEffect(() => () => historyRead.current?.abort(), []);
  useEffect(() => {
    if (!canAcquire) return;
    const controller = new AbortController();
    let reading = false;
    async function read() {
      if (reading) return;
      reading = true;
      try {
        const workers = await desk.api.get<WorkerStatus[]>(path + '/workers', controller.signal);
        if (!controller.signal.aborted) {
          setWorker(workers.find((item) => item.stream_id === stream.id));
          setWorkerError('');
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setWorker(undefined);
          setWorkerError(message(error));
        }
      } finally {
        reading = false;
      }
    }
    void read();
    const timer = setInterval(() => void read(), 3000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [desk.api, path, stream.id, revision, canAcquire]);

  async function command(action: 'start' | 'stop' | 'finish') {
    const label =
      action === 'start'
        ? 'Start acquisition'
        : action === 'stop'
          ? 'Stop acquisition'
          : 'Finish session';
    await desk.run(label, async () => {
      const workerPath = path + '/streams/' + encodePathSegment(stream.id);
      try {
        if (action === 'start') {
          if (session.state === 'draft') await desk.api.post(path + '/start', {});
          await desk.api.post(workerPath + '/start', {});
        } else if (action === 'stop') {
          await desk.api.post(workerPath + '/stop', { mode: 'graceful' });
        } else {
          await desk.api.post(path + '/stop', {});
        }
      } finally {
        setWorker(undefined);
        setRevision((value) => value + 1);
        await desk.reloadSessions();
      }
    }, []);
  }

  async function loadHistory() {
    historyRead.current?.abort();
    const controller = new AbortController();
    historyRead.current = controller;
    const sources = await desk.run(
      'Load history',
      () =>
        desk.api.get<Sweep[]>(
          path + '/sweeps?' + new URLSearchParams({ stream_id: stream.id, limit: '32' }),
          controller.signal,
        ),
      [],
      controller.signal,
    );
    if (sources && !controller.signal.aborted)
      setHistory(
        sources.filter((s) => s.session_id === session.id && s.stream_id === stream.id).reverse(),
      );
  }

  const liveRows = frozen?.rows ?? desk.live.rows;
  const rows = useMemo(
    () =>
      liveRows.filter((sweep) => sweep.session_id === session.id && sweep.stream_id === stream.id),
    [liveRows, session.id, stream.id],
  );
  const latest = rows.at(-1);
  const survey = latest?.rf_explorer?.survey;
  const surveySession = stream.settings?.survey_step_hz === 10000 || !!survey;
  const entire = hasPanorama && surveySession && view === 'pass';
  const planned =
    typeof stream.settings?.start_hz === 'number' && typeof stream.settings?.stop_hz === 'number'
      ? { start_hz: stream.settings.start_hz, stop_hz: stream.settings.stop_hz }
      : undefined;
  const workerReadback = rfExplorerReadback(worker?.current);
  const reported =
    workerReadback?.confirmed && workerReadback.active_board
      ? workerReadback
      : rows.findLast((row) => row.rf_explorer?.confirmed && row.rf_explorer.active_board)
          ?.rf_explorer;
  const lastMeasured = useMemo(() => {
    if (!cursor) return;
    let last = cursor.row.interval.ended_at;
    const frequency = cursor.row.frequencies_hz[cursor.index];
    for (const row of desk.live.rows)
      if (
        row.session_id === session.id &&
        row.stream_id === stream.id &&
        row.interval.ended_at > last &&
        row.frequencies_hz.includes(frequency)
      )
        last = row.interval.ended_at;
    return last;
  }, [cursor, desk.live.rows, session.id, stream.id]);
  async function inspectCursor() {
    if (!cursor) return;
    sourceRead.current?.abort();
    const controller = new AbortController();
    sourceRead.current = controller;
    setSourceError('');
    try {
      const sw = await readRfOriginal(
        desk.api,
        session.id,
        stream.id,
        cursor.row.id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const at = cursor.row.source_indices[cursor.index];
      if (
        Number(sw.frequencies_hz[at]) !== cursor.row.frequencies_hz[cursor.index] ||
        sw.levels[at] !== cursor.row.levels[cursor.index]
      )
        throw Error('Source evidence mismatch');
      openOriginal(sw, at);
    } catch (cause) {
      if (!controller.signal.aborted) setSourceError(message(cause));
    }
  }
  const acquisitionError = workerError || worker?.last_error;
  return (
    <>
      <div className="project-strip">
        <div className="project-meta">
          <strong>
            {stream.port} · {titleCase(session.state)}
          </strong>
          <small>{worker ? titleCase(worker.state) : t('Unavailable')}</small>
        </div>
        <div className="actions">
          <button
            className="primary"
            disabled={
              desk.busy ||
              !canAcquire ||
              surveyUnsupported ||
              !worker ||
              worker.alive ||
              !['draft', 'running'].includes(session.state)
            }
            onClick={() => void command('start')}
          >
            {t('Start acquisition')}
          </button>
          <button disabled={desk.busy || !worker?.alive} onClick={() => void command('stop')}>
            {t('Stop acquisition')}
          </button>
          <button
            disabled={desk.busy || !worker || worker.alive || session.state !== 'running'}
            onClick={() => void command('finish')}
          >
            {t('Finish session')}
          </button>
        </div>
      </div>
      {(surveySession || planned) && (
        <details className="rf-draft-preview">
          <summary>{t('Acquisition preview')}</summary>
          <RfPlanPreview bounds={planned} survey={surveySession} readback={reported} />
        </details>
      )}
      {surveyUnsupported && (
        <p className="muted">
          {t('Starting this survey requires a core with 10 kHz survey support.')}
        </p>
      )}
      {acquisitionError && (
        <p className="error-text" role="alert">
          {t(acquisitionError)}
        </p>
      )}
      <div className="live-grid">
        <Panel
          title={t(entire ? 'Survey panorama' : 'Live spectrum')}
          className="spectrum-panel"
          aside={<span className="live-badge">{t(entire ? 'Archive' : desk.live.state)}</span>}
        >
          <div className="actions rf-view-controls">
            {hasPanorama && surveySession && (
              <div role="group" aria-label={t('Spectrum view')} className="rf-switch">
                <button aria-pressed={!entire} onClick={() => setView('window')}>
                  {t('Current window')}
                </button>
                <button aria-pressed={entire} onClick={() => setView('pass')}>
                  {t('Entire pass')}
                </button>
              </div>
            )}
            <button
              aria-pressed={!!frozen}
              onClick={() =>
                setFrozen(
                  frozen
                    ? undefined
                    : { rows: desk.live.rows, presentation: desk.live.presentation },
                )
              }
            >
              {t(frozen ? 'Resume display' : 'Pause display')}
            </button>
          </div>
          {frozen && (
            <p className="muted">{t('Display paused. Acquisition continues independently.')}</p>
          )}
          {entire ? (
            <RfExplorerPass
              hold={hold}
              setHold={setHold}
              api={desk.api}
              session={session.id}
              stream={stream.id}
              running={session.state === 'running' && (readOnly || !worker || worker.alive)}
              paused={!!frozen}
              openOriginal={openOriginal}
              readOnly={readOnly}
              bookmarks={desk.capabilities?.features.frequency_bookmarks === true}
            />
          ) : (
            <>
              {latest && (
                <p className="muted">
                  {survey && t('Segment peak') + ' '}
                  {hz(latest.peak.frequency_hz)} · {latest.peak.level} {latest.level_unit} ·{' '}
                  {latest.original_point_count} {t('original points')}
                </p>
              )}
              {latest && (
                <RfExplorerSettings
                  readback={latest.rf_explorer}
                  frequencies={latest.frequencies_hz}
                />
              )}
              <Spectrum
                sweep={latest}
                liveSources={frozen ? undefined : { rows, presentation: desk.live.presentation }}
                onSelectPoint={(row, index) => {
                  sourceRead.current?.abort();
                  setSourceError('');
                  setCursor({ row, index });
                }}
                belowPlot={
                  <>
                    <h3>{t(survey ? 'Waterfall across the planned range' : 'Waterfall')}</h3>
                    {survey && (
                      <p className="muted">
                        {hz(survey.start_hz)} – {hz(survey.stop_hz)} ·{' '}
                        {t('Unmeasured frequencies are black in each row.')}
                      </p>
                    )}
                    <Waterfall
                      rows={rows}
                      band={survey && { low: survey.start_hz, high: survey.stop_hz }}
                    />
                  </>
                }
              />
              <div
                className="source-footer"
                role="group"
                aria-label={t('Latest saved measurement')}
              >
                <span data-testid="source-id">
                  {latest?.id ?? t('Waiting for a committed sweep')}
                </span>
                <span>
                  {utc(latest?.interval.ended_at)}
                  {latest && (
                    <>
                      {' '}
                      · {t('Source age')} <SourceAge at={latest.interval.ended_at} />
                    </>
                  )}
                </span>
              </div>
              {cursor && (
                <div className="rf-evidence">
                  <strong>
                    {hz(cursor.row.frequencies_hz[cursor.index])} ·{' '}
                    {cursor.row.levels[cursor.index]} {cursor.row.level_unit}
                  </strong>
                  <p className="mono break">
                    {cursor.row.id} · {t('Source index')} {cursor.row.source_indices[cursor.index]}
                  </p>
                  <p>
                    {utc(cursor.row.interval.started_at)} → {utc(cursor.row.interval.ended_at)}
                  </p>
                  <p>
                    {t('Selected frequency last measured')} · {utc(lastMeasured)} ·{' '}
                    <SourceAge at={lastMeasured} />
                  </p>
                  <button onClick={() => void inspectCursor()}>{t('Open original sweep')}</button>
                </div>
              )}
              {sourceError && (
                <p className="error-text" role="alert">
                  {t(sourceError)}
                </p>
              )}
            </>
          )}
        </Panel>
        <Panel title={t('Sweep history')} aside={<small>{t('Latest 32 saved sweeps')}</small>}>
          <button disabled={desk.busy} onClick={() => void loadHistory()}>
            {t('Load history')}
          </button>
          {history && (
            <div className="history-list">
              {history.length ? (
                history.map((sweep) => (
                  <div className="history-row" key={sweep.id}>
                    <button
                      className="text-button history-select"
                      aria-label={t('Original sweep') + ' · ' + sweep.id}
                      onClick={() => openOriginal(sweep)}
                    >
                      <strong>{utc(sweep.interval.ended_at)}</strong>
                      <small>
                        {sweep.levels.length} {t('original points')} · {sweep.level_unit} ·{' '}
                        {sweep.id}
                      </small>
                    </button>
                  </div>
                ))
              ) : (
                <p className="empty">{t('No saved sweeps for this stream')}</p>
              )}
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}

export function RfExplorer({
  desk,
  select,
  openOriginal,
  actionStatus,
  readOnly = false,
}: {
  readOnly?: boolean;
  desk: Desk;
  select: (id: string) => void;
  openOriginal: OpenRfOriginal;
  actionStatus: ReactNode;
}) {
  const [creating, setCreating] = useState(false);
  return (
    <main id="main">
      <div className="page-heading">
        <h1 data-testid="workspace-heading">RF Explorer 3G Combo</h1>
        {!readOnly && desk.capabilities?.features.rf_explorer_3g_spectrum === true && (
          <button className="primary" disabled={desk.busy} onClick={() => setCreating(true)}>
            {t('New spectrum session')}
          </button>
        )}
      </div>
      <p className="muted">
        {t(
          !readOnly && desk.capabilities?.features.rf_explorer_3g_spectrum === true
            ? 'Receive-only serial acquisition · hardware validation pending'
            : 'Archived RF Explorer measurements',
        )}
      </p>
      {!desk.rfSessions.length && <p className="empty">{t('No RF Explorer sessions')}</p>}
      {desk.rfSessions.length > 0 && (
        <label>
          {t('Spectrum session')}
          <select
            disabled={desk.busy}
            value={desk.rfSession?.id ?? ''}
            onChange={(e) => select(e.target.value)}
          >
            {desk.rfSessions.map((s) => (
              <option value={s.id} key={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {actionStatus}
      {desk.rfSession && (
        <Acquisition
          key={JSON.stringify([desk.rfSession.id, desk.rfSession.streams[0].id])}
          desk={desk}
          session={desk.rfSession}
          readOnly={readOnly}
          openOriginal={openOriginal}
        />
      )}
      {creating && (
        <RfExplorerNewSession desk={desk} select={select} close={() => setCreating(false)} />
      )}
    </main>
  );
}
