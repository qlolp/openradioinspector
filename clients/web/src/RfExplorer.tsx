import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Spectrum, Waterfall } from './charts';
import { hz, Modal, Panel, titleCase, utc } from './components';
import { t } from './i18n';
import { rfExplorerSession, type RfExplorerReadback } from './rf-explorer';
import { type Desk, message } from './useDesk';
import type { Session, Sweep, WorkerStatus } from './types';
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
      <div>
        <dt>{t('Reported detector')}</dt>
        <dd>{readback?.detector ?? t('Unavailable')}</dd>
      </div>
      <div>
        <dt>{t('Reported RBW')}</dt>
        <dd>{hz(readback?.rbw_hz)}</dd>
      </div>
      <div>
        <dt>{t('Bin spacing')}</dt>
        <dd>{hz(readback?.step_hz)}</dd>
      </div>
    </dl>
  );
}

function NewSession({
  desk,
  select,
  close,
}: {
  desk: Desk;
  select: (id: string) => void;
  close: () => void;
}) {
  const [name, setName] = useState('');
  const [port, setPort] = useState('');
  const [selection, setSelection] = useState('follow');
  const [start, setStart] = useState('');
  const [stop, setStop] = useState('');
  const [error, setError] = useState('');
  const supportsSurvey = desk.capabilities?.features.rf_explorer_10khz_survey === true;
  const mode = supportsSurvey || selection === 'fixed' ? selection : 'follow';
  const survey = mode === 'survey-module' || mode === 'survey-range';
  const customWindow = mode === 'fixed' || mode === 'survey-range';
  function chooseMode(next: string) {
    setSelection(next);
    setError('');
  }
  function create() {
    try {
      const body = rfExplorerSession(name, port, { survey, ...(customWindow && { start, stop }) });
      setError('');
      void desk.run(
        'Create session',
        async () => {
          const session = await desk.api.post<Session>('/sessions', body);
          select(session.id);
          close();
          await desk.reloadSessions();
        },
        [],
      );
    } catch (cause) {
      setError(message(cause));
    }
  }
  return (
    <Modal label={t('New spectrum session')} close={close}>
      <h2>{t('New spectrum session')}</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create();
        }}
      >
        <label>
          {t('Session name')}
          <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          {t('RF Explorer serial port')}
          <input
            required
            value={port}
            placeholder="/dev/ttyUSB0, COM3"
            onChange={(e) => setPort(e.target.value)}
          />
        </label>
        {supportsSurvey ? (
          <label>
            {t('Acquisition mode')}
            <select value={mode} onChange={(e) => chooseMode(e.target.value)}>
              <option value="follow">{t('Follow analyzer window')}</option>
              <option value="fixed">{t('Set a sweep window')}</option>
              <option value="survey-module">{t('10 kHz survey · full active module')}</option>
              <option value="survey-range">{t('10 kHz survey · selected range')}</option>
            </select>
          </label>
        ) : (
          <label className="check">
            <input
              type="checkbox"
              checked={customWindow}
              onChange={(e) => chooseMode(e.target.checked ? 'fixed' : 'follow')}
            />
            {t('Set a sweep window')}
          </label>
        )}
        {customWindow && (
          <div className="form-grid">
            <label>
              {t('Start frequency (Hz)')}
              <input
                type="number"
                required
                min={1000}
                step={1000}
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              {t('Stop frequency (Hz)')}
              <input
                type="number"
                required
                min={1000}
                step={1000}
                value={stop}
                onChange={(e) => setStop(e.target.value)}
              />
            </label>
          </div>
        )}
        {mode === 'follow' && (
          <p className="muted">{t('Acquisition follows the active window on the analyzer.')}</p>
        )}
        {mode === 'survey-module' && (
          <p className="muted">
            {t(
              'The analyzer reports the active module’s limits. Each pass measures this range one segment at a time.',
            )}
          </p>
        )}
        {mode === 'survey-range' && (
          <p className="muted">
            {t(
              'Both endpoints are included. Enter multiples of 1,000 Hz with a span of at least 1.11 MHz in 10 kHz steps.',
            )}
          </p>
        )}
        {error && (
          <p className="error-text" role="alert">
            {t(error)}
          </p>
        )}
        {desk.action.state === 'Error' && (
          <p className="error-text" role="alert">
            {t(desk.action.text)}
          </p>
        )}
        <div className="actions">
          <button className="primary" disabled={desk.busy}>
            {t('Create session')}
          </button>
          <button type="button" disabled={desk.busy} onClick={close}>
            {t('Cancel')}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function Acquisition({
  desk,
  session,
  openOriginal,
}: {
  desk: Desk;
  session: Session;
  openOriginal: (sweep: Sweep) => void;
}) {
  const [stream] = session.streams;
  const surveyUnsupported =
    stream.settings?.survey_step_hz === 10_000 &&
    desk.capabilities?.features.rf_explorer_10khz_survey !== true;
  const path = '/sessions/' + encodePathSegment(session.id);
  const [worker, setWorker] = useState<WorkerStatus>();
  const [workerError, setWorkerError] = useState('');
  const [revision, setRevision] = useState(0);
  const [history, setHistory] = useState<Sweep[]>();
  const historyRead = useRef<AbortController | null>(null);
  useEffect(() => () => historyRead.current?.abort(), []);
  useEffect(() => {
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
  }, [desk.api, path, stream.id, revision]);

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

  const rows = useMemo(
    () =>
      desk.live.rows.filter(
        (sweep) => sweep.session_id === session.id && sweep.stream_id === stream.id,
      ),
    [desk.live.rows, session.id, stream.id],
  );
  const latest = rows.at(-1);
  const survey = latest?.rf_explorer?.survey;
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
          title={t('Live spectrum')}
          className="spectrum-panel"
          aside={<span className="live-badge">{t(desk.live.state)}</span>}
        >
          {latest && (
            <p className="muted">
              {survey && t('Segment peak') + ' '}
              {hz(latest.peak.frequency_hz)} · {latest.peak.level} {latest.level_unit} ·{' '}
              {latest.original_point_count} {t('original points')}
            </p>
          )}
          {latest && (
            <RfExplorerSettings readback={latest.rf_explorer} frequencies={latest.frequencies_hz} />
          )}
          <Spectrum
            sweep={latest}
            liveSources={{ rows, presentation: desk.live.presentation }}
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
          <div className="source-footer" role="group" aria-label={t('Latest saved measurement')}>
            <span data-testid="source-id">{latest?.id ?? t('Waiting for a committed sweep')}</span>
            <span>{utc(latest?.interval.ended_at)}</span>
          </div>
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
}: {
  desk: Desk;
  select: (id: string) => void;
  openOriginal: (sweep: Sweep) => void;
  actionStatus: ReactNode;
}) {
  const [creating, setCreating] = useState(false);
  return (
    <main id="main">
      <div className="page-heading">
        <h1 data-testid="workspace-heading">RF Explorer 3G Combo</h1>
        <button className="primary" disabled={desk.busy} onClick={() => setCreating(true)}>
          {t('New spectrum session')}
        </button>
      </div>
      <p className="muted">{t('Receive-only serial acquisition · hardware validation pending')}</p>
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
          key={desk.rfSession.id}
          desk={desk}
          session={desk.rfSession}
          openOriginal={openOriginal}
        />
      )}
      {creating && <NewSession desk={desk} select={select} close={() => setCreating(false)} />}
    </main>
  );
}
