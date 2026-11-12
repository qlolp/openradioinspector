import { t, useLocale } from './i18n';
import { LocaleControls } from './LocaleControls';
import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { Spectrum, Waterfall } from './charts';
import { originalJSON, encodePathSegment } from './wire';
import { Modal, Panel, hz, titleCase, utc } from './components';
import { Prepare } from './Prepare';
import { Research } from './Research';
import { remember, save, useDesk, type Desk } from './useDesk';
import type { Connection } from './transport';
import type { MonitoringEvent, Page, Sweep } from './types';

function PreparationPanel({ desk }: { desk: Desk }) {
  const [name, setName] = useState(() => t('Ordinary environment')),
    [location, setLocation] = useState(''),
    [confirm, setConfirm] = useState(false);
  const [audioInputs, setAudioInputs] = useState<{ index: number; name: string }[]>([]),
    [audioIndex, setAudioIndex] = useState(''),
    [audioConfirmed, setAudioConfirmed] = useState(false);
  useEffect(() => {
    setLocation(desk.project?.site || '');
    setConfirm(false);
    setAudioInputs([]);
    setAudioIndex('');
    setAudioConfirmed(false);
  }, [desk.project?.id]);
  const collecting = desk.project?.state === 'collecting',
    progress = desk.preflight?.baseline_collection;
  return (
    <Panel
      title={t('Receive preparation')}
      eyebrow={t('PREFLIGHT → BASELINE → GUARD')}
      aside={<span className="badge">{t('RF receive only')}</span>}
    >
      <div className="preflight-heading">
        <span className={'dot ' + (desk.preflight?.ready ? 'ok' : 'warn')} />
        <strong>
          {desk.preflight
            ? desk.preflight.ready
              ? t('Preflight passed')
              : t('Preparation needs attention')
            : t('Checking core readiness')}
        </strong>
        <button
          className="text-button"
          disabled={desk.busy || !desk.project}
          onClick={() => void desk.run('Run preflight', () => desk.refresh(['preflight']), [])}
        >
          {t('Run preflight')}
        </button>
      </div>
      <ul className="check-list">
        {desk.preflight?.checks.map((check) => (
          <li key={check.name}>
            <span className={check.ok ? 'check-ok' : 'error-text'}>{check.ok ? '✓' : '!'}</span>
            <span>
              <strong>{titleCase(check.name)}</strong>
              <small>{check.detail}</small>
            </span>
          </li>
        ))}
      </ul>
      {desk.project?.preparation && (
        <p className="muted">
          {t('Receive path:') + ' '}
          {desk.project.preparation.antenna_notes}
        </p>
      )}
      <div className="baseline-status">
        <strong>
          {desk.preflight?.baseline_ready
            ? t('Quiet reference ready')
            : t('Baseline not confirmed')}
        </strong>
        <span className="mono">
          {progress?.sweeps || 0}
          {' ' + t('sweeps ·') + ' '}
          {(progress?.elapsed_seconds || 0).toFixed(1)}
          {' ' + t('s')}
        </span>
      </div>
      <p className="muted">
        {t('Collect at least') + ' '}
        {progress?.minimum_sweeps || 20}
        {' ' + t('complete sweeps over')} {progress?.minimum_seconds || 60}
        {' ' + t('seconds in the ordinary environment.')}
      </p>
      <div className="actions">
        <button
          disabled={
            desk.busy || !desk.project || desk.guard?.active || collecting || !desk.preflight?.ready
          }
          onClick={() => void desk.command('preparation/start')}
        >
          {t('Start receive collection')}
        </button>
        <button
          disabled={desk.busy || !collecting}
          onClick={() => void desk.command('preparation/stop')}
        >
          {t('Stop collection')}
        </button>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void desk.command('baselines', { name, location, confirm });
        }}
      >
        <div className="form-grid">
          <label>
            {t('Baseline name')}
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            {t('Baseline location')}
            <input
              required
              maxLength={200}
              value={location}
              onChange={(e) => setLocation(e.target.value)}
            />
          </label>
        </div>
        <label className="check">
          <input
            type="checkbox"
            required
            checked={confirm}
            onChange={(e) => setConfirm(e.target.checked)}
          />
          {t('I confirm this baseline represents the ordinary environment')}
        </label>
        <button
          disabled={
            desk.busy || !desk.project || desk.guard?.active || !progress?.ready || !confirm
          }
        >
          {t('Confirm baseline')}
        </button>
      </form>
      <details>
        <summary>{t('AR-DV1 analog audio input')}</summary>
        <p className="muted">
          {t(
            'Choose the node’s analog input explicitly. Captured audio is stored by the core; hardware validation has not run.',
          )}
        </p>
        <p>
          {t('Current input:') + ' '}
          {desk.project?.audio?.name || t('Not configured')}
        </p>
        <button
          disabled={desk.busy || desk.guard?.active}
          onClick={() =>
            void desk.run(
              'List node audio inputs',
              async () => {
                const result = await desk.api.get<{ devices: { index: number; name: string }[] }>(
                  '/monitoring-projects/audio-inputs',
                );
                setAudioInputs(result.devices);
              },
              [],
            )
          }
        >
          {t('List node audio inputs')}
        </button>
        <label>
          {t('Analog input')}
          <select value={audioIndex} onChange={(e) => setAudioIndex(e.target.value)}>
            <option value="">{t('Choose input')}</option>
            {audioInputs.map((input) => (
              <option key={input.index} value={input.index}>
                {input.index}: {input.name}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={audioConfirmed}
            onChange={(e) => setAudioConfirmed(e.target.checked)}
          />
          {t('This is the AR-DV1 analog output')}
        </label>
        <div className="actions">
          <button
            disabled={desk.busy || desk.guard?.active || !audioIndex || !audioConfirmed}
            onClick={() => {
              const device = audioInputs.find((input) => String(input.index) === audioIndex);
              if (device)
                void desk.command('configure', {
                  audio: { device: device.index, name: device.name, confirm: true },
                });
            }}
          >
            {t('Save analog input')}
          </button>
          <button
            disabled={desk.busy || desk.guard?.active || !desk.project?.audio}
            onClick={() => void desk.command('configure', { audio: null })}
          >
            {t('Disable audio capture')}
          </button>
        </div>
      </details>
    </Panel>
  );
}

function AllowedPanel({ desk }: { desk: Desk }) {
  const [name, setName] = useState(''),
    [start, setStart] = useState(''),
    [end, setEnd] = useState(''),
    [watch, setWatch] = useState(true);
  return (
    <Panel
      title={t('Allowed signals')}
      eyebrow={t('CORE DETECTION POLICY')}
      aside={
        <span className="badge">
          {desk.allowed.length}
          {' ' + t('loaded')}
        </span>
      }
    >
      <div className="allowed-list">
        {desk.allowed.map((item) => (
          <div key={item.id}>
            <strong>{item.name}</strong>
            <span className="mono">
              {hz(item.start_hz)}–{hz(item.end_hz)}
            </span>
            <small>{item.watch_changes ? t('Watch changes') : t('Authorized range')}</small>
          </div>
        ))}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void desk.run(
            'Add allowed signal',
            async () => {
              await desk.api.project(desk.projectId, 'allowed', {
                name,
                start_hz: Number(start),
                end_hz: Number(end),
                watch_changes: watch,
              });
              setName('');
            },
            ['allowed'],
          );
        }}
      >
        <label>
          {t('Signal name')}
          <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="form-grid">
          <label>
            {t('Allowed start (Hz)')}
            <input
              required
              pattern="[0-9]+"
              inputMode="numeric"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label>
            {t('Allowed end (Hz)')}
            <input
              required
              pattern="[0-9]+"
              inputMode="numeric"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={watch} onChange={(e) => setWatch(e.target.checked)} />
          {t('Watch changes inside this range')}
        </label>
        <button disabled={desk.busy || !desk.project}>{t('Add allowed signal')}</button>
      </form>
    </Panel>
  );
}

export function App({
  transport,
  onConnectionSettings,
}: {
  transport: Connection;
  onConnectionSettings?: () => void;
}) {
  useLocale();
  const desk = useDesk(transport);
  const [workspace, setWorkspace] = useState(() => remember('workspace', 'monitoring'));
  const [theme, setTheme] = useState(() => remember('theme', 'system'));
  const [wizard, setWizard] = useState(false),
    [event, setEvent] = useState<MonitoringEvent>(),
    [original, setOriginal] = useState<Sweep>();
  const [originalOffset, setOriginalOffset] = useState(0),
    [audioUrl, setAudioUrl] = useState(''),
    [audioError, setAudioError] = useState('');
  const [disposition, setDisposition] = useState('unreviewed'),
    [comment, setComment] = useState('');
  const audio = useRef<HTMLAudioElement>(null),
    dialogGeneration = useRef(0),
    dialogAbort = useRef(new AbortController());
  const p = desk.project,
    unavailable = desk.live.unsupported,
    latest = unavailable ? undefined : desk.live.rows.at(-1),
    receiver = desk.live.observation;
  const receiverWorker = desk.guard?.workers.find(
    (worker) => worker.stream_id === p?.receiver_stream_id,
  );
  const analyzerWorker = desk.guard?.workers.find(
    (worker) => worker.stream_id === p?.analyzer_stream_id,
  );
  const setScreen = (value: string) => {
    setWorkspace(value);
    save('workspace', value);
  };
  function legacy(event: MouseEvent<HTMLAnchorElement>) {
    if (transport.native) {
      event.preventDefault();
      void desk.run('Open legacy UI', () => transport.openLegacy(), []);
    }
  }
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    save('theme', theme);
  }, [theme]);
  useEffect(() => {
    setEvent(undefined);
    setOriginal(undefined);
    setAudioUrl('');
    dialogGeneration.current++;
    dialogAbort.current.abort();
    dialogAbort.current = new AbortController();
  }, [desk.projectId, desk.connectionVersion]);
  useEffect(() => {
    return () => {
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
  }, [audioUrl]);
  useEffect(() => {
    const keyboard = (e: KeyboardEvent) => {
      if (e.altKey && !e.ctrlKey && !e.metaKey && !document.querySelector('dialog[open]')) {
        if (e.code === 'KeyM') {
          e.preventDefault();
          setScreen('monitoring');
        }
        if (e.code === 'KeyR') {
          e.preventDefault();
          setScreen('research');
        }
      }
    };
    window.addEventListener('keydown', keyboard);
    return () => window.removeEventListener('keydown', keyboard);
  }, []);
  function closeEvent() {
    setEvent(undefined);
    setAudioUrl('');
    setAudioError('');
    dialogGeneration.current++;
    dialogAbort.current.abort();
    dialogAbort.current = new AbortController();
  }
  async function openEvent(summary: MonitoringEvent) {
    closeEvent();
    setEvent(summary);
    setDisposition(summary.disposition || 'unreviewed');
    setComment(summary.operator_comment || '');
    const generation = dialogGeneration.current,
      signal = dialogAbort.current.signal;
    await desk.run(
      'Load event details',
      async () => {
        const value = await desk.api.project<MonitoringEvent>(
          desk.projectId,
          'events/' + encodePathSegment(summary.id),
          undefined,
          signal,
        );
        if (generation === dialogGeneration.current) setEvent(value);
      },
      [],
      signal,
    );
  }
  async function openOriginal(id: string) {
    const generation = dialogGeneration.current,
      signal = dialogAbort.current.signal;
    await desk.run(
      'Load original sweep',
      async () => {
        const value = await desk.api.project<Sweep>(
          desk.projectId,
          'history/' + encodePathSegment(id),
          undefined,
          signal,
        );
        if (generation === dialogGeneration.current) {
          setOriginal(value);
          setOriginalOffset(0);
        }
      },
      [],
      signal,
    );
  }
  async function eventOriginal() {
    if (!event) return;
    const source = event.source_refs?.at(-1);
    if (source) {
      await openOriginal(source.sweep_id);
      return;
    }
    const generation = dialogGeneration.current,
      signal = dialogAbort.current.signal;
    await desk.run(
      'Load original event source',
      async () => {
        const sources = await desk.api.project<Page<{ sweep_id: string }>>(
          desk.projectId,
          'events/' + encodePathSegment(event.id) + '/sources?limit=1',
          undefined,
          signal,
        );
        if (generation !== dialogGeneration.current || signal.aborted) return;
        if (!sources.items[0]) throw Error('No original sweep is available for this event');
        const value = await desk.api.project<Sweep>(
          desk.projectId,
          'history/' + encodePathSegment(sources.items[0].sweep_id),
          undefined,
          signal,
        );
        if (generation === dialogGeneration.current) {
          setOriginal(value);
          setOriginalOffset(0);
        }
      },
      [],
      signal,
    );
  }
  async function playAudio() {
    if (!event) return;
    const generation = dialogGeneration.current,
      signal = dialogAbort.current.signal;
    setAudioError('');
    await desk.run(
      'Load event audio',
      async () => {
        const blob = await transport.audio(
          `/api/v1/monitoring-projects/${encodePathSegment(desk.projectId)}/events/${encodePathSegment(event.id)}/audio`,
          signal,
        );
        if (generation === dialogGeneration.current) {
          const url = URL.createObjectURL(blob);
          setAudioUrl(url);
          if (audio.current) {
            audio.current.src = url;
            void audio.current.play().catch((error) => setAudioError(error.message));
          }
        }
      },
      [],
      signal,
    );
  }
  function downloadOriginal() {
    if (!original) return;
    const url = URL.createObjectURL(
      new Blob([originalJSON(original)], { type: 'application/json' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = 'sweep-' + original.id.replace(/[^\w-]/g, '_') + '.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const coverage = p?.coverage || 'stopped';
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        {t('Skip to workspace')}
      </a>
      <header className="topbar">
        <a
          className="brand"
          href="/modern/"
          aria-label={t('OpenRadioInspector home')}
          onClick={(event) => {
            if (transport.native) {
              event.preventDefault();
              setScreen('monitoring');
            }
          }}
        >
          <span className="brand-mark" aria-hidden="true">
            ◈
          </span>
          <span>
            {t('OPENRADIO')}
            <span className="brand-light">{t('INSPECTOR')}</span>
            <small>{t('SPECTRUM OPERATIONS')}</small>
          </span>
        </a>
        <div className="topbar-right">
          <LocaleControls />
          <span className="node-pill">
            <span className={'dot ' + (desk.health ? 'ok' : 'warn')} />
            {desk.health ? t('Core online') : t('Connecting core')}
          </span>
          <label className="theme-select">
            <span className="sr-only">{t('Theme')}</span>
            <select value={theme} onChange={(e) => setTheme(e.target.value)}>
              <option value="system">{t('System')}</option>
              <option value="dark">{t('Dark')}</option>
              <option value="light">{t('Light')}</option>
            </select>
          </label>
          {onConnectionSettings && (
            <button className="text-button" onClick={onConnectionSettings}>
              {t('Connection settings')}
            </button>
          )}
          <a className="legacy-link" href="/" onClick={legacy}>
            {t('Legacy UI ↗')}
          </a>
        </div>
      </header>
      <div className="workspace-bar">
        <nav aria-label={t('Workspace')}>
          <button aria-pressed={workspace === 'monitoring'} onClick={() => setScreen('monitoring')}>
            {t('Monitoring')}
          </button>
          <button aria-pressed={workspace === 'research'} onClick={() => setScreen('research')}>
            {t('Research')}
          </button>
        </nav>
        <span className="workspace-note">{t('Local view · global guard state')}</span>
        <span className="shortcut">{t('Alt M / Alt R')}</span>
      </div>
      <main id="main">
        <div className="page-heading">
          <div>
            <div className="eyebrow">{t('RECEIVE · OBSERVE · INVESTIGATE')}</div>
            <h1 data-testid="workspace-heading">{t('Spectrum desk')}</h1>
            <p className="muted">{t('One core. Independent instruments. Original evidence.')}</p>
          </div>
          <button
            className="primary"
            onClick={() => setWizard(true)}
            disabled={desk.busy || !desk.connected}
          >
            {t('＋ Prepare project')}
          </button>
        </div>
        <div className="project-strip">
          <label>
            {t('Project')}
            <select
              aria-label={t('Project')}
              data-testid="project-select"
              disabled={desk.busy}
              value={desk.projectId}
              onChange={(e) => desk.setProjectId(e.target.value)}
            >
              <option value="">{t('Choose a monitoring project')}</option>
              {desk.projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <div className="project-meta">
            <strong>{p?.site || t('Select or prepare a project')}</strong>
            <small>
              {p?.active_session_id
                ? t('Session') + ' ' + p.active_session_id
                : t('Selection stays on this client')}
            </small>
          </div>
          <div className="actions">
            <button
              className="icon-button"
              aria-label={t('Previous projects')}
              disabled={desk.busy || !desk.projectsHistory.length}
              onClick={() => void desk.previousProjects()}
            >
              ←
            </button>
            <button
              className="icon-button"
              aria-label={t('Next projects')}
              disabled={desk.busy || !desk.projectsCursor}
              onClick={() => void desk.nextProjects()}
            >
              →
            </button>
          </div>
        </div>
        <div className={'guard-strip ' + (desk.guard?.active ? 'guard-active' : '')}>
          <div className="guard-label">
            <span
              className={'guard-symbol ' + (desk.guard?.active ? 'active' : '')}
              aria-hidden="true"
            >
              ◇
            </span>
            <div>
              <small>{t('GLOBAL GUARD')}</small>
              <strong data-testid="guard-state">{titleCase(p?.state || t('unavailable'))}</strong>
            </div>
          </div>
          <div className="coverage">
            <strong>
              {coverage === 'wide'
                ? t('Broad coverage active')
                : coverage === 'narrow'
                  ? t('Narrow search · broad coverage paused')
                  : t('Coverage: {state}', { state: titleCase(coverage) })}
            </strong>
            <small>
              {desk.guard?.error ||
                p?.last_error ||
                (desk.guard?.active
                  ? t('Core owns the receive pair')
                  : t('Guard is stopped · viewing does not arm instruments'))}
            </small>
          </div>
          <div className="actions">
            <button
              className="primary"
              disabled={
                desk.busy ||
                !p ||
                desk.guard?.active ||
                !desk.preflight?.ready ||
                !desk.preflight?.baseline_ready
              }
              onClick={() => void desk.command('arm')}
            >
              {t('Arm guard')}
            </button>
            <button
              className="danger"
              disabled={desk.busy || !desk.guard?.active}
              onClick={() => void desk.command('disarm')}
            >
              {t('Disarm guard')}
            </button>
          </div>
        </div>
        <div
          className={'action-state ' + desk.action.state.toLowerCase()}
          data-testid="action-state"
          role={desk.action.state === 'Error' ? 'alert' : 'status'}
          aria-live="polite"
        >
          <strong>{t(desk.action.state)}</strong>
          <span>{t(desk.action.text)}</span>
          {desk.action.state === 'Error' && (
            <button onClick={() => void desk.resync()} disabled={desk.busy}>
              {t('Resync')}
            </button>
          )}
        </div>
        <div className="live-grid">
          <Panel
            title={t('Live spectrum')}
            eyebrow={t('tinySA ULTRA · ORIGINAL dBm')}
            className="spectrum-panel"
            aside={
              <span className="live-badge" data-testid="live-state">
                <span className={'dot ' + (desk.live.state.startsWith('Live') ? 'ok' : 'warn')} />
                {t(desk.live.state)}
              </span>
            }
          >
            <div className="spectrum-metrics">
              <div>
                <small>{t('PEAK FREQUENCY')}</small>
                <strong className="mono">{hz(latest?.peak.frequency_hz)}</strong>
              </div>
              <div>
                <small>{t('PEAK LEVEL')}</small>
                <strong className="mono">
                  {latest
                    ? latest.peak.level.toFixed(1) + ' ' + latest.level_unit
                    : unavailable
                      ? t('Not plotted')
                      : t('— dBm')}
                </strong>
              </div>
              <div>
                <small>{t('COMMITTED EVENT')}</small>
                <strong className="mono">#{desk.live.sequence}</strong>
              </div>
            </div>
            {unavailable ? (
              <div className="plot-unavailable" role="status" data-testid="unsupported-source">
                <strong>{t('Source frequency outside supported display range')}</strong>
                <p>{t('Original measurements remain available without numeric conversion.')}</p>
                <p className="mono">{hz(unavailable.frequency_hz)}</p>
                <button type="button" onClick={() => void openOriginal(unavailable.id)}>
                  {t('Original sweep')}
                </button>
              </div>
            ) : (
              <Spectrum sweep={latest} />
            )}
            <div className="chart-caption">
              <span>{t('Frequency · MHz')}</span>
              <span>
                {unavailable?.point_count ?? latest?.original_point_count ?? 0}
                {' ' + t('original points · display only')}
              </span>
            </div>
            <Waterfall rows={desk.live.rows} />
            <div className="source-footer">
              <span data-testid="source-id">
                {unavailable?.id || latest?.id || t('Waiting for a committed sweep')}
              </span>
              <span>{utc(unavailable?.interval.ended_at ?? latest?.interval.ended_at)}</span>
            </div>
            {desk.live.gaps > 0 && (
              <p className="callout">
                {desk.live.gaps}
                {' ' +
                  t(
                    'client sequence/backlog gap(s). Replaying the archive from the last applied event; the render buffer is bounded.',
                  )}
              </p>
            )}
          </Panel>
          <div className="side-stack">
            <Panel
              title={t('AR-DV1')}
              eyebrow={t('INDEPENDENT RECEIVER')}
              aside={
                <span className={'badge ' + (receiverWorker?.alive ? 'good' : '')}>
                  {receiverWorker?.alive
                    ? t('Receiving')
                    : titleCase(receiverWorker?.state || t('idle'))}
                </span>
              }
            >
              <div className="receiver-frequency mono">{hz(receiver?.frequency_hz)}</div>
              <div className="receiver-stats">
                <div>
                  <small>{t('MODE')}</small>
                  <strong>{receiver?.mode || '—'}</strong>
                </div>
                <div>
                  <small>{t('RAW LEVEL')}</small>
                  <strong className="mono">
                    {receiver?.level ?? '—'}{' '}
                    <small>{receiver?.level_unit || t('s_meter_count')}</small>
                  </strong>
                </div>
                <div>
                  <small>{t('SQUELCH')}</small>
                  <strong>
                    {receiver?.squelch_open == null
                      ? t('Unknown')
                      : receiver.squelch_open
                        ? t('Open')
                        : t('Closed')}
                  </strong>
                </div>
                <div>
                  <small>{t('INVESTIGATION')}</small>
                  <strong>{desk.guard?.receiver_busy ? t('In progress') : t('Idle')}</strong>
                </div>
              </div>
              <p className="muted">
                {t('S-meter counts retain their original unit. No dBm conversion is applied.')}
              </p>
              <div className="device-footer">
                <span className="mono">
                  {receiverWorker?.measurements || 0}
                  {' ' + t('observations')}
                </span>
                <span>{receiverWorker?.adapter || t('No active worker')}</span>
              </div>
              {receiverWorker?.last_error && (
                <p className="error-text">{receiverWorker.last_error}</p>
              )}
            </Panel>
            <Panel title={t('Node & evidence')} eyebrow={t('CORE STATUS')}>
              <dl className="status-list">
                <div>
                  <dt>{t('tinySA')}</dt>
                  <dd>
                    {analyzerWorker?.alive
                      ? t('Receiving')
                      : titleCase(analyzerWorker?.state || t('idle'))}
                  </dd>
                </div>
                <div>
                  <dt>{t('Archive')}</dt>
                  <dd>{desk.capabilities?.session_storage || t('Unknown')}</dd>
                </div>
                <div>
                  <dt>{t('Event evidence')}</dt>
                  <dd>
                    {desk.events.length}
                    {' ' + t('loaded')}
                  </dd>
                </div>
                <div>
                  <dt>{t('Audio input')}</dt>
                  <dd>{desk.guard?.audio_configured ? t('Configured') : t('Not configured')}</dd>
                </div>
                <div>
                  <dt>{t('Hardware validation')}</dt>
                  <dd className="warning-text">{t('Not run')}</dd>
                </div>
              </dl>
              <details>
                <summary>{t('Capability limits')}</summary>
                <p className="muted">
                  {t(
                    'IQ, network identities, extended RF ranges, generator control, and unverified digital modes are unavailable in this client. Advanced analysis, exports, task scheduling, and instrument diagnostics remain in the',
                  )}{' '}
                  <a href="/" onClick={legacy}>
                    {t('legacy UI')}
                  </a>
                  .
                </p>
              </details>
            </Panel>
          </div>
        </div>
        <section hidden={workspace !== 'monitoring'} aria-label={t('Monitoring workspace')}>
          <Panel
            title={t('Detected events')}
            eyebrow={t('COMMITTED EVIDENCE')}
            aside={
              <button
                disabled={desk.busy || !p}
                onClick={() =>
                  void desk.run('Refresh detected events', () => desk.refresh(['events']), [])
                }
              >
                {t('Refresh events')}
              </button>
            }
          >
            {desk.events.length ? (
              <div className="event-table">
                <div className="event-table-header">
                  <span>{t('Frequency / event')}</span>
                  <span>{t('First seen · UTC')}</span>
                  <span>{t('Peak')}</span>
                  <span>{t('Analysis')}</span>
                  <span>{t('Evidence')}</span>
                </div>
                {desk.events.map((item) => (
                  <div className="event-row" data-testid="event-row" key={item.id}>
                    <div>
                      <strong className="mono">{hz(item.center_frequency_hz)}</strong>
                      <small>
                        {titleCase(item.kind)} · {t(item.state)}
                      </small>
                    </div>
                    <div className="mono event-time">{utc(item.first_seen)}</div>
                    <div className="mono">
                      {item.peak_level.toFixed(1)}
                      {' ' + t('dBm')}
                    </div>
                    <div>
                      <span className="badge">
                        {titleCase(item.analysis?.state || t('pending'))}
                      </span>
                      <small>
                        {item.analysis?.audio ? t('Audio available') : t('No audio evidence')}
                      </small>
                    </div>
                    <div>
                      <button disabled={desk.busy} onClick={() => void openEvent(item)}>
                        {t('Details')}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="empty">
                {t('No detected events. Collection and detection belong to the core.')}
              </p>
            )}
          </Panel>
          <div className="preparation-grid">
            <PreparationPanel desk={desk} />
            <AllowedPanel desk={desk} />
          </div>
        </section>
        <section hidden={workspace !== 'research'} aria-label={t('Research workspace')}>
          <Research
            desk={desk}
            openOriginal={(id) => {
              void openOriginal(id);
            }}
          />
        </section>
        <footer className="page-footer">
          <span>{t('OpenRadioInspector · v1 contract · receive only')}</span>
          <span>{t('UTC timestamps · integer Hz · original units')}</span>
        </footer>
      </main>
      {wizard && <Prepare desk={desk} close={() => setWizard(false)} />}
      {event && (
        <Modal label={t('Event details')} close={closeEvent}>
          <div className="eyebrow">{t('EVENT EVIDENCE')}</div>
          <h2>{t('Event details')}</h2>
          <div className="event-frequency mono">{hz(event.center_frequency_hz)}</div>
          <p>
            {titleCase(event.kind)} · {event.peak_level}
            {' ' + t('dBm ·') + ' '}
            {event.source_count}
            {' ' + t('source sweeps')}
          </p>
          <dl className="status-list">
            <div>
              <dt>{t('First seen')}</dt>
              <dd className="mono">{utc(event.first_seen)}</dd>
            </div>
            <div>
              <dt>{t('Last seen')}</dt>
              <dd className="mono">{utc(event.last_seen)}</dd>
            </div>
            <div>
              <dt>{t('Analysis')}</dt>
              <dd>{titleCase(event.analysis?.state || t('pending'))}</dd>
            </div>
            <div>
              <dt>{t('Original event ID')}</dt>
              <dd className="mono break">{event.id}</dd>
            </div>
          </dl>
          <div className="actions">
            <button className="primary" disabled={desk.busy} onClick={() => void eventOriginal()}>
              {t('Original sweep')}
            </button>
            <button disabled={desk.busy || !event.analysis?.audio} onClick={() => void playAudio()}>
              {t('Play audio')}
            </button>
          </div>
          <audio
            ref={audio}
            controls
            src={audioUrl || undefined}
            preload="none"
            aria-label={t('Event audio evidence')}
          />
          {audioError && <p className="error-text">{t(audioError)}</p>}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const generation = dialogGeneration.current,
                signal = dialogAbort.current.signal;
              void desk.run(
                'Review event',
                async () => {
                  const value = await desk.api.project<MonitoringEvent>(
                    desk.projectId,
                    'events/' + encodePathSegment(event.id) + '/review',
                    { disposition, comment },
                    signal,
                  );
                  if (generation === dialogGeneration.current && !signal.aborted) setEvent(value);
                },
                ['events'],
              );
            }}
          >
            <label>
              {t('Disposition')}
              <select value={disposition} onChange={(e) => setDisposition(e.target.value)}>
                {['unreviewed', 'authorized', 'false_positive', 'investigate'].map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('Operator comment')}
              <textarea
                maxLength={1000}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
              />
            </label>
            <div className="actions">
              <button disabled={desk.busy}>{t('Save review')}</button>
              <button type="button" onClick={closeEvent}>
                {t('Close event')}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {original && (
        <Modal label={t('Original sweep')} close={() => setOriginal(undefined)} wide>
          <div className="eyebrow">{t('IMMUTABLE SOURCE')}</div>
          <h2>{t('Original sweep')}</h2>
          <p>
            <strong>
              {original.levels.length}
              {' ' + t('original points')}
            </strong>{' '}
            · {original.level_unit}
            {' ' + t('· no client resampling')}
          </p>
          <p className="mono break">
            {original.id}
            <br />
            {utc(original.interval.started_at)} → {utc(original.interval.ended_at)}
          </p>
          <div className="original-table">
            <table>
              <thead>
                <tr>
                  <th>{t('Source index')}</th>
                  <th>{t('Frequency · Hz')}</th>
                  <th>
                    {t('Level ·') + ' '}
                    {original.level_unit}
                  </th>
                </tr>
              </thead>
              <tbody>
                {original.levels.slice(originalOffset, originalOffset + 100).map((level, i) => (
                  <tr key={i}>
                    <td>{originalOffset + i}</td>
                    <td>{original.frequencies_hz[originalOffset + i]}</td>
                    <td>{level}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="actions">
            <button
              disabled={originalOffset === 0}
              onClick={() => setOriginalOffset((x) => Math.max(0, x - 100))}
            >
              {t('Previous points')}
            </button>
            <button
              disabled={originalOffset + 100 >= original.levels.length}
              onClick={() => setOriginalOffset((x) => x + 100)}
            >
              {t('Next points')}
            </button>
            <button onClick={downloadOriginal}>{t('Download original JSON')}</button>
            <button onClick={() => setOriginal(undefined)}>{t('Close original')}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
