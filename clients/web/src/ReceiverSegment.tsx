import { useEffect, useState } from 'react';
import { Panel, hz, titleCase, utc } from './components';
import { t } from './i18n';
import { encodePathSegment } from './wire';
import type { Desk } from './useDesk';
import type { Observation, Session, WorkerStatus } from './types';

export function segmentPoints(start: number, stop: number, step: number): number | undefined {
  if (
    ![start, stop, step].every((n) => Number.isSafeInteger(n) && n % 10 === 0) ||
    start < 100000 ||
    stop > 1300000000 ||
    stop <= start ||
    step < 10 ||
    step > 1300000000
  )
    return;
  const points = Math.ceil((stop - start) / step) + 1;
  return points <= 1000 ? points : undefined;
}

export function ReceiverSegment({ desk }: { desk: Desk }) {
  const [start, setStart] = useState('100000000'),
    [stop, setStop] = useState('100100000');
  const [step, setStep] = useState('25000'),
    [settle, setSettle] = useState('0.015');
  const [chosen, setChosen] = useState(''),
    [revision, setRevision] = useState(0);
  const [snapshot, setSnapshot] = useState<{
    sessionId: string;
    revision: number;
    connectionVersion: number;
    sessionState?: string;
    worker?: WorkerStatus;
    last?: Observation;
    error?: string;
  }>();
  const project = desk.project;
  const receiver =
    project?.profile?.receiver ??
    desk.sessions
      .find((s) => s.id === project?.active_session_id)
      ?.streams.find((stream) => stream.id === project?.receiver_stream_id);
  const available = desk.capabilities?.features.ardv1_sampled_scope === true;
  const sessions = desk.sessions.filter(
    (s) =>
      project &&
      s.metadata?.receiver_segment_project === project.id &&
      s.streams.length === 1 &&
      s.streams[0].settings?.fast_scan,
  );
  const session =
    sessions.find((s) => s.id === chosen) ??
    sessions.find((s) => s.state === 'running') ??
    sessions.at(-1);
  const stream = session?.streams[0],
    sessionId = session?.id;
  const path = sessionId ? '/sessions/' + encodePathSegment(sessionId) : '';
  const settings = stream?.settings.fast_scan as
    { start_hz: number; stop_hz: number; step_hz: number } | undefined;
  const measured =
    snapshot?.sessionId === sessionId &&
    snapshot?.revision === revision &&
    snapshot?.connectionVersion === desk.connectionVersion &&
    snapshot?.sessionState === session?.state
      ? snapshot
      : undefined;
  const points = segmentPoints(Number(start), Number(stop), Number(step));
  const canPrepare =
    available &&
    !!project &&
    receiver?.settings?.adapter === 'ardv1-serial' &&
    !desk.busy &&
    points !== undefined &&
    settle.trim() !== '' &&
    Number.isFinite(Number(settle)) &&
    Number(settle) >= 0 &&
    Number(settle) <= 1;
  useEffect(() => {
    if (!available || !sessionId || !stream || !path) return;
    const abort = new AbortController();
    let reading = false;
    async function read() {
      if (reading) return;
      reading = true;
      try {
        const [workers, observations] = await Promise.all([
          desk.api.get<WorkerStatus[]>(path + '/workers', abort.signal),
          desk.api.get<Observation[]>(
            path + '/observations?' + new URLSearchParams({ stream_id: stream!.id, limit: '1' }),
            abort.signal,
          ),
        ]);
        if (!abort.signal.aborted)
          setSnapshot({
            sessionId: sessionId!,
            revision,
            connectionVersion: desk.connectionVersion,
            sessionState: session?.state,
            worker: workers.find((w) => w.stream_id === stream!.id),
            last: observations.at(-1),
          });
      } catch (reason) {
        if (!abort.signal.aborted)
          setSnapshot({
            sessionId: sessionId!,
            revision,
            connectionVersion: desk.connectionVersion,
            sessionState: session?.state,
            error: reason instanceof Error ? reason.message : t('Unavailable'),
          });
      } finally {
        reading = false;
      }
    }
    void read();
    const timer = session?.state === 'running' ? setInterval(() => void read(), 3000) : undefined;
    return () => {
      clearInterval(timer);
      abort.abort();
    };
  }, [
    available,
    desk.api,
    desk.connectionVersion,
    path,
    sessionId,
    stream?.id,
    session?.state,
    revision,
  ]);
  if (!available || !receiver || (receiver.settings.adapter !== 'ardv1-serial' && !session))
    return null;

  async function prepare() {
    if (!canPrepare || !receiver || !project) return;
    await desk.run(
      'Prepare receiver segment',
      async () => {
        const value = await desk.api.post<Session>('/sessions', {
          name: [...(project.name + ' · ' + t('AR-DV1 segment'))].slice(0, 200).join(''),
          metadata: { receiver_segment_project: project.id, receive_only: true },
          streams: [
            {
              device: receiver.device,
              role: 'receiver',
              port: receiver.port,
              level_unit: 's_meter_count',
              capabilities: ['receiver'],
              settings: {
                adapter: 'ardv1-serial',
                receiver_profile: 'ardv1-monitor-v1',
                mode: 'FM',
                period_s: 0,
                max_consecutive_errors: 1,
                ...(receiver.settings.baud ? { baud: receiver.settings.baud } : {}),
                frequency_hz: Number(start),
                fast_scan: {
                  start_hz: Number(start),
                  stop_hz: Number(stop),
                  step_hz: Number(step),
                  settle_s: Number(settle),
                  strategy: 'auto',
                },
              },
            },
          ],
        });
        setChosen(value.id);
        await desk.reloadSessions();
      },
      [],
    );
  }

  async function command(action: 'start' | 'stop' | 'finish') {
    if (!session || !stream) return;
    const outcome = await desk.run(
      action === 'start'
        ? 'Start receiver segment'
        : action === 'stop'
          ? 'Stop receiver segment'
          : 'Finish segment session',
      async () => {
        const workerPath = path + '/streams/' + encodePathSegment(stream.id);
        try {
          if (action === 'start') {
            if (session.state !== 'running') await desk.api.post(path + '/start', {});
            await desk.api.post(workerPath + '/start', {});
          } else if (action === 'finish') await desk.api.post(path + '/stop', {});
          else await desk.api.post(workerPath + '/stop', { mode: 'graceful' });
        } finally {
          await desk.reloadSessions();
        }
        return true;
      },
      [],
    );
    // Read back the core's lifecycle and any committed point after success or a partial start.
    setRevision((n) => n + 1);
    return outcome;
  }
  const worker = measured?.worker;
  const ownsGuardPort = desk.guard?.active || project?.state === 'collecting';
  return (
    <Panel title={t('AR-DV1 segment')}>
      <p className="muted">
        {t(
          'A separate FM session uses the receiver port until the last point or an explicit stop. Each committed reading keeps its UTC interval and S-meter unit.',
        )}
      </p>
      <p>
        {t('Receiver port')} · <code>{receiver.port}</code>
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void prepare();
        }}
      >
        <div className="form-grid">
          <label>
            {t('Segment start (Hz)')}
            <input
              type="number"
              min="100000"
              max="1300000000"
              step="10"
              required
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label>
            {t('Segment end (Hz)')}
            <input
              type="number"
              min="100000"
              max="1300000000"
              step="10"
              required
              value={stop}
              onChange={(e) => setStop(e.target.value)}
            />
          </label>
          <label>
            {t('Step (Hz)')}
            <input
              type="number"
              min="10"
              max="1300000000"
              step="10"
              required
              value={step}
              onChange={(e) => setStep(e.target.value)}
            />
          </label>
          <label>
            {t('Settling time (s)')}
            <input
              type="number"
              min="0"
              max="1"
              step="any"
              required
              value={settle}
              onChange={(e) => setSettle(e.target.value)}
            />
          </label>
        </div>
        <p className="muted">
          {points === undefined
            ? t('Choose 2–1000 points on the receiver’s 10 Hz tuning grid.')
            : t('Segment points') + ' · ' + points}
        </p>
        <div className="actions">
          <button disabled={!canPrepare}>{t('Prepare receiver segment')}</button>
          <button
            type="button"
            disabled={desk.busy}
            onClick={() =>
              void desk.run(
                'Refresh saved segments',
                async () => {
                  await desk.reloadSessions();
                  setRevision((value) => value + 1);
                },
                [],
              )
            }
          >
            {t('Refresh saved segments')}
          </button>
        </div>
      </form>
      {session && (
        <>
          <label>
            {t('Saved receiver segment')}
            <select value={session.id} onChange={(e) => setChosen(e.target.value)}>
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} · {s.id}
                </option>
              ))}
            </select>
          </label>
          {settings && (
            <p className="mono">
              {hz(settings.start_hz)} – {hz(settings.stop_hz)} · {hz(settings.step_hz)}
            </p>
          )}
          <p>
            {titleCase(worker?.state ?? session.state)} · {t('Committed points')}{' '}
            {worker?.measurements ?? '—'} /{' '}
            {settings
              ? (segmentPoints(settings.start_hz, settings.stop_hz, settings.step_hz) ?? '—')
              : '—'}
          </p>
          {measured?.last && (
            <p className="mono">
              {hz(measured.last.frequency_hz)} · {measured.last.level ?? '—'}{' '}
              {t(measured.last.level_unit)} · {utc(measured.last.interval.ended_at)}
            </p>
          )}
          {(measured?.error || worker?.last_error) && (
            <p role="alert">{measured?.error || worker?.last_error}</p>
          )}
          {ownsGuardPort && (
            <p className="muted">
              {t('The monitoring project owns the receiver while collection or guard is active.')}
            </p>
          )}
          <div className="actions">
            <button
              disabled={
                desk.busy ||
                ownsGuardPort ||
                !measured ||
                !!measured.error ||
                worker?.alive ||
                !!worker?.measurements ||
                !['draft', 'running'].includes(session.state)
              }
              onClick={() => void command('start')}
            >
              {t('Start receiver segment')}
            </button>
            <button
              disabled={
                desk.busy ||
                !worker?.alive ||
                desk.capabilities?.features.graceful_device_stop !== true
              }
              onClick={() => void command('stop')}
            >
              {t('Stop receiver segment')}
            </button>
            <button
              disabled={
                desk.busy ||
                session.state !== 'running' ||
                !measured ||
                !!measured.error ||
                worker?.alive
              }
              onClick={() => void command('finish')}
            >
              {t('Finish segment session')}
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}
