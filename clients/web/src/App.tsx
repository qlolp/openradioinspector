import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { Spectrum, Waterfall } from './charts';
import { Modal, Panel, hz, titleCase, utc } from './components';
import { Prepare } from './Prepare';
import { Research } from './Research';
import { remember, save, useDesk, type Desk } from './useDesk';
import type { Connection } from './transport';
import type { MonitoringEvent, Page, Sweep } from './types';

function PreparationPanel({ desk }: { desk: Desk }) {
  const [name, setName] = useState('Ordinary environment'),
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
      title="Receive preparation"
      eyebrow="PREFLIGHT → BASELINE → GUARD"
      aside={<span className="badge">RF receive only</span>}
    >
      <div className="preflight-heading">
        <span className={'dot ' + (desk.preflight?.ready ? 'ok' : 'warn')} />
        <strong>
          {desk.preflight
            ? desk.preflight.ready
              ? 'Preflight passed'
              : 'Preparation needs attention'
            : 'Checking core readiness'}
        </strong>
        <button
          className="text-button"
          disabled={desk.busy || !desk.project}
          onClick={() => void desk.run('Run preflight', () => desk.refresh(['preflight']), [])}
        >
          Run preflight
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
        <p className="muted">Receive path: {desk.project.preparation.antenna_notes}</p>
      )}
      <div className="baseline-status">
        <strong>
          {desk.preflight?.baseline_ready ? 'Quiet reference ready' : 'Baseline not confirmed'}
        </strong>
        <span className="mono">
          {progress?.sweeps || 0} sweeps · {(progress?.elapsed_seconds || 0).toFixed(1)} s
        </span>
      </div>
      <p className="muted">
        Collect at least {progress?.minimum_sweeps || 20} complete sweeps over{' '}
        {progress?.minimum_seconds || 60} seconds in the ordinary environment.
      </p>
      <div className="actions">
        <button
          disabled={
            desk.busy || !desk.project || desk.guard?.active || collecting || !desk.preflight?.ready
          }
          onClick={() => void desk.command('preparation/start')}
        >
          Start receive collection
        </button>
        <button
          disabled={desk.busy || !collecting}
          onClick={() => void desk.command('preparation/stop')}
        >
          Stop collection
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
            Baseline name
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Baseline location
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
          I confirm this baseline represents the ordinary environment
        </label>
        <button
          disabled={
            desk.busy || !desk.project || desk.guard?.active || !progress?.ready || !confirm
          }
        >
          Confirm baseline
        </button>
      </form>
      <details>
        <summary>AR-DV1 analog audio input</summary>
        <p className="muted">
          Choose the node’s analog input explicitly. Captured audio is stored by the core; hardware
          validation has not run.
        </p>
        <p>Current input: {desk.project?.audio?.name || 'Not configured'}</p>
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
          List node audio inputs
        </button>
        <label>
          Analog input
          <select value={audioIndex} onChange={(e) => setAudioIndex(e.target.value)}>
            <option value="">Choose input</option>
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
          This is the AR-DV1 analog output
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
            Save analog input
          </button>
          <button
            disabled={desk.busy || desk.guard?.active || !desk.project?.audio}
            onClick={() => void desk.command('configure', { audio: null })}
          >
            Disable audio capture
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
      title="Allowed signals"
      eyebrow="CORE DETECTION POLICY"
      aside={<span className="badge">{desk.allowed.length} loaded</span>}
    >
      <div className="allowed-list">
        {desk.allowed.map((item) => (
          <div key={item.id}>
            <strong>{item.name}</strong>
            <span className="mono">
              {hz(item.start_hz)}–{hz(item.end_hz)}
            </span>
            <small>{item.watch_changes ? 'Watch changes' : 'Authorized range'}</small>
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
          Signal name
          <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="form-grid">
          <label>
            Allowed start (Hz)
            <input
              required
              pattern="[0-9]+"
              inputMode="numeric"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label>
            Allowed end (Hz)
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
          Watch changes inside this range
        </label>
        <button disabled={desk.busy || !desk.project}>Add allowed signal</button>
      </form>
    </Panel>
  );
}

export function App({ transport }: { transport: Connection }) {
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
  const [node, setNode] = useState(() => remember('node', 'http://127.0.0.1:8770')),
    [token, setToken] = useState('');
  const audio = useRef<HTMLAudioElement>(null),
    dialogGeneration = useRef(0),
    dialogAbort = useRef(new AbortController());
  const p = desk.project,
    latest = desk.live.rows.at(-1),
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
        if (e.key.toLowerCase() === 'm') {
          e.preventDefault();
          setScreen('monitoring');
        }
        if (e.key.toLowerCase() === 'r') {
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
    const generation = dialogGeneration.current;
    await desk.run(
      'Load event details',
      async () => {
        const value = await desk.api.project<MonitoringEvent>(
          desk.projectId,
          'events/' + encodeURIComponent(summary.id),
          undefined,
          dialogAbort.current.signal,
        );
        if (generation === dialogGeneration.current) setEvent(value);
      },
      [],
    );
  }
  async function openOriginal(id: string) {
    const generation = dialogGeneration.current;
    await desk.run(
      'Load original sweep',
      async () => {
        const value = await desk.api.project<Sweep>(
          desk.projectId,
          'history/' + encodeURIComponent(id),
          undefined,
          dialogAbort.current.signal,
        );
        if (generation === dialogGeneration.current) {
          setOriginal(value);
          setOriginalOffset(0);
        }
      },
      [],
    );
  }
  async function eventOriginal() {
    if (!event) return;
    const source = event.source_refs?.at(-1);
    if (source) {
      await openOriginal(source.sweep_id);
      return;
    }
    const generation = dialogGeneration.current;
    await desk.run(
      'Load original event source',
      async () => {
        const sources = await desk.api.project<Page<{ sweep_id: string }>>(
          desk.projectId,
          'events/' + encodeURIComponent(event.id) + '/sources?limit=1',
          undefined,
          dialogAbort.current.signal,
        );
        if (!sources.items[0]) throw Error('No original sweep is available for this event');
        const value = await desk.api.project<Sweep>(
          desk.projectId,
          'history/' + encodeURIComponent(sources.items[0].sweep_id),
          undefined,
          dialogAbort.current.signal,
        );
        if (generation === dialogGeneration.current) {
          setOriginal(value);
          setOriginalOffset(0);
        }
      },
      [],
    );
  }
  async function playAudio() {
    if (!event) return;
    const generation = dialogGeneration.current;
    setAudioError('');
    await desk.run(
      'Load event audio',
      async () => {
        const blob = await transport.audio(
          `/api/v1/monitoring-projects/${encodeURIComponent(desk.projectId)}/events/${encodeURIComponent(event.id)}/audio`,
          dialogAbort.current.signal,
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
    );
  }
  function downloadOriginal() {
    if (!original) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(original)], { type: 'application/json' }),
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
        Skip to workspace
      </a>
      <header className="topbar">
        <a
          className="brand"
          href="/modern/"
          aria-label="OpenRadioInspector home"
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
            OPENRADIO<span className="brand-light">INSPECTOR</span>
            <small>SPECTRUM OPERATIONS</small>
          </span>
        </a>
        <div className="topbar-right">
          <span className="node-pill">
            <span className={'dot ' + (desk.health ? 'ok' : 'warn')} />
            {desk.health ? 'Core online' : 'Connecting core'}
          </span>
          <label className="theme-select">
            <span className="sr-only">Theme</span>
            <select value={theme} onChange={(e) => setTheme(e.target.value)}>
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
          </label>
          <a className="legacy-link" href="/" onClick={legacy}>
            Legacy UI ↗
          </a>
        </div>
      </header>
      <div className="workspace-bar">
        <nav aria-label="Workspace">
          <button aria-pressed={workspace === 'monitoring'} onClick={() => setScreen('monitoring')}>
            Monitoring
          </button>
          <button aria-pressed={workspace === 'research'} onClick={() => setScreen('research')}>
            Research
          </button>
        </nav>
        <span className="workspace-note">Local view · global guard state</span>
        <span className="shortcut">Alt M / Alt R</span>
      </div>
      <main id="main">
        <div className="page-heading">
          <div>
            <div className="eyebrow">RECEIVE · OBSERVE · INVESTIGATE</div>
            <h1>Spectrum desk</h1>
            <p className="muted">One core. Independent instruments. Original evidence.</p>
          </div>
          <button
            className="primary"
            onClick={() => setWizard(true)}
            disabled={desk.busy || !desk.connected}
          >
            ＋ Prepare project
          </button>
        </div>
        {transport.native && (
          <Panel title="Core node" eyebrow="NATIVE HTTP BRIDGE">
            <form
              className="node-form"
              onSubmit={(e) => {
                e.preventDefault();
                void desk.connect(node, token).then(() => setToken(''));
              }}
            >
              <label>
                Node URL
                <input
                  type="url"
                  required
                  value={node}
                  onChange={(e) => setNode(e.target.value)}
                  placeholder="http://127.0.0.1:8770"
                />
              </label>
              <label>
                Bearer token (optional)
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <button disabled={desk.busy}>Connect node</button>
            </form>
            <small className="muted">
              Credentials remain in native process memory for this session. Configure TLS on remote
              nodes.
            </small>
          </Panel>
        )}
        <div className="project-strip">
          <label>
            Project
            <select
              aria-label="Project"
              disabled={desk.busy}
              value={desk.projectId}
              onChange={(e) => desk.setProjectId(e.target.value)}
            >
              <option value="">Choose a monitoring project</option>
              {desk.projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <div className="project-meta">
            <strong>{p?.site || 'Select or prepare a project'}</strong>
            <small>
              {p?.active_session_id
                ? 'Session ' + p.active_session_id
                : 'Selection stays on this client'}
            </small>
          </div>
          <div className="actions">
            <button
              className="icon-button"
              aria-label="Previous projects"
              disabled={desk.busy || !desk.projectsHistory.length}
              onClick={() => void desk.previousProjects()}
            >
              ←
            </button>
            <button
              className="icon-button"
              aria-label="Next projects"
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
              <small>GLOBAL GUARD</small>
              <strong data-testid="guard-state">{titleCase(p?.state || 'unavailable')}</strong>
            </div>
          </div>
          <div className="coverage">
            <strong>
              {coverage === 'wide'
                ? 'Broad coverage active'
                : coverage === 'narrow'
                  ? 'Narrow search · broad coverage paused'
                  : titleCase(coverage) + ' coverage'}
            </strong>
            <small>
              {desk.guard?.error ||
                p?.last_error ||
                (desk.guard?.active
                  ? 'Core owns the receive pair'
                  : 'Guard is stopped · viewing does not arm instruments')}
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
              Arm guard
            </button>
            <button
              className="danger"
              disabled={desk.busy || !desk.guard?.active}
              onClick={() => void desk.command('disarm')}
            >
              Disarm guard
            </button>
          </div>
        </div>
        <div
          className={'action-state ' + desk.action.state.toLowerCase()}
          data-testid="action-state"
          role={desk.action.state === 'Error' ? 'alert' : 'status'}
          aria-live="polite"
        >
          <strong>{desk.action.state}</strong>
          <span>{desk.action.text}</span>
          {desk.action.state === 'Error' && (
            <button onClick={() => void desk.resync()} disabled={desk.busy}>
              Resync
            </button>
          )}
        </div>
        <div className="live-grid">
          <Panel
            title="Live spectrum"
            eyebrow="tinySA ULTRA · ORIGINAL dBm"
            className="spectrum-panel"
            aside={
              <span className="live-badge" data-testid="live-state">
                <span className={'dot ' + (desk.live.state.startsWith('Live') ? 'ok' : 'warn')} />
                {desk.live.state}
              </span>
            }
          >
            <div className="spectrum-metrics">
              <div>
                <small>PEAK FREQUENCY</small>
                <strong className="mono">{hz(latest?.peak.frequency_hz)}</strong>
              </div>
              <div>
                <small>PEAK LEVEL</small>
                <strong className="mono">
                  {latest ? latest.peak.level.toFixed(1) + ' ' + latest.level_unit : '— dBm'}
                </strong>
              </div>
              <div>
                <small>COMMITTED EVENT</small>
                <strong className="mono">#{desk.live.sequence}</strong>
              </div>
            </div>
            <Spectrum sweep={latest} />
            <div className="chart-caption">
              <span>Frequency · MHz</span>
              <span>{latest?.original_point_count || 0} original points · display only</span>
            </div>
            <Waterfall rows={desk.live.rows} />
            <div className="source-footer">
              <span data-testid="source-id">{latest?.id || 'Waiting for a committed sweep'}</span>
              <span>{utc(latest?.interval.ended_at)}</span>
            </div>
            {desk.live.gaps > 0 && (
              <p className="callout">
                {desk.live.gaps} client sequence/backlog gap(s). Snapshot resynchronization
                preserves source IDs; the render buffer is bounded.
              </p>
            )}
          </Panel>
          <div className="side-stack">
            <Panel
              title="AR-DV1"
              eyebrow="INDEPENDENT RECEIVER"
              aside={
                <span className={'badge ' + (receiverWorker?.alive ? 'good' : '')}>
                  {receiverWorker?.alive ? 'Receiving' : titleCase(receiverWorker?.state || 'idle')}
                </span>
              }
            >
              <div className="receiver-frequency mono">{hz(receiver?.frequency_hz)}</div>
              <div className="receiver-stats">
                <div>
                  <small>MODE</small>
                  <strong>{receiver?.mode || '—'}</strong>
                </div>
                <div>
                  <small>RAW LEVEL</small>
                  <strong className="mono">
                    {receiver?.level ?? '—'}{' '}
                    <small>{receiver?.level_unit || 's_meter_count'}</small>
                  </strong>
                </div>
                <div>
                  <small>SQUELCH</small>
                  <strong>
                    {receiver?.squelch_open == null
                      ? 'Unknown'
                      : receiver.squelch_open
                        ? 'Open'
                        : 'Closed'}
                  </strong>
                </div>
                <div>
                  <small>INVESTIGATION</small>
                  <strong>{desk.guard?.receiver_busy ? 'In progress' : 'Idle'}</strong>
                </div>
              </div>
              <p className="muted">
                S-meter counts retain their original unit. No dBm conversion is applied.
              </p>
              <div className="device-footer">
                <span className="mono">{receiverWorker?.measurements || 0} observations</span>
                <span>{receiverWorker?.adapter || 'No active worker'}</span>
              </div>
              {receiverWorker?.last_error && (
                <p className="error-text">{receiverWorker.last_error}</p>
              )}
            </Panel>
            <Panel title="Node & evidence" eyebrow="CORE STATUS">
              <dl className="status-list">
                <div>
                  <dt>tinySA</dt>
                  <dd>
                    {analyzerWorker?.alive
                      ? 'Receiving'
                      : titleCase(analyzerWorker?.state || 'idle')}
                  </dd>
                </div>
                <div>
                  <dt>Archive</dt>
                  <dd>{desk.capabilities?.session_storage || 'Unknown'}</dd>
                </div>
                <div>
                  <dt>Event evidence</dt>
                  <dd>{desk.events.length} loaded</dd>
                </div>
                <div>
                  <dt>Audio input</dt>
                  <dd>{desk.guard?.audio_configured ? 'Configured' : 'Not configured'}</dd>
                </div>
                <div>
                  <dt>Hardware validation</dt>
                  <dd className="warning-text">Not run</dd>
                </div>
              </dl>
              <details>
                <summary>Capability limits</summary>
                <p className="muted">
                  IQ, network identities, extended RF ranges, generator control, and unverified
                  digital modes are unavailable in this client. Advanced analysis, exports, task
                  scheduling, and instrument diagnostics remain in the{' '}
                  <a href="/" onClick={legacy}>
                    legacy UI
                  </a>
                  .
                </p>
              </details>
            </Panel>
          </div>
        </div>
        <section hidden={workspace !== 'monitoring'} aria-label="Monitoring workspace">
          <Panel
            title="Detected events"
            eyebrow="COMMITTED EVIDENCE"
            aside={
              <button
                disabled={desk.busy || !p}
                onClick={() =>
                  void desk.run('Refresh detected events', () => desk.refresh(['events']), [])
                }
              >
                Refresh events
              </button>
            }
          >
            {desk.events.length ? (
              <div className="event-table">
                <div className="event-table-header">
                  <span>Frequency / event</span>
                  <span>First seen · UTC</span>
                  <span>Peak</span>
                  <span>Analysis</span>
                  <span>Evidence</span>
                </div>
                {desk.events.map((item) => (
                  <div className="event-row" data-testid="event-row" key={item.id}>
                    <div>
                      <strong className="mono">{hz(item.center_frequency_hz)}</strong>
                      <small>
                        {titleCase(item.kind)} · {item.state}
                      </small>
                    </div>
                    <div className="mono event-time">{utc(item.first_seen)}</div>
                    <div className="mono">{item.peak_level.toFixed(1)} dBm</div>
                    <div>
                      <span className="badge">{titleCase(item.analysis?.state || 'pending')}</span>
                      <small>
                        {item.analysis?.audio ? 'Audio available' : 'No audio evidence'}
                      </small>
                    </div>
                    <div>
                      <button disabled={desk.busy} onClick={() => void openEvent(item)}>
                        Details
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="empty">
                No detected events. Collection and detection belong to the core.
              </p>
            )}
          </Panel>
          <div className="preparation-grid">
            <PreparationPanel desk={desk} />
            <AllowedPanel desk={desk} />
          </div>
        </section>
        <section hidden={workspace !== 'research'} aria-label="Research workspace">
          <Research
            desk={desk}
            openOriginal={(id) => {
              void openOriginal(id);
            }}
          />
        </section>
        <footer className="page-footer">
          <span>OpenRadioInspector · v1 contract · receive only</span>
          <span>UTC timestamps · integer Hz · original units</span>
        </footer>
      </main>
      {wizard && <Prepare desk={desk} close={() => setWizard(false)} />}
      {event && (
        <Modal label="Event details" close={closeEvent}>
          <div className="eyebrow">EVENT EVIDENCE</div>
          <h2>Event details</h2>
          <div className="event-frequency mono">{hz(event.center_frequency_hz)}</div>
          <p>
            {titleCase(event.kind)} · {event.peak_level} dBm · {event.source_count} source sweeps
          </p>
          <dl className="status-list">
            <div>
              <dt>First seen</dt>
              <dd className="mono">{utc(event.first_seen)}</dd>
            </div>
            <div>
              <dt>Last seen</dt>
              <dd className="mono">{utc(event.last_seen)}</dd>
            </div>
            <div>
              <dt>Analysis</dt>
              <dd>{titleCase(event.analysis?.state || 'pending')}</dd>
            </div>
            <div>
              <dt>Original event ID</dt>
              <dd className="mono break">{event.id}</dd>
            </div>
          </dl>
          <div className="actions">
            <button className="primary" disabled={desk.busy} onClick={() => void eventOriginal()}>
              Original sweep
            </button>
            <button disabled={desk.busy || !event.analysis?.audio} onClick={() => void playAudio()}>
              Play audio
            </button>
          </div>
          <audio
            ref={audio}
            controls
            src={audioUrl || undefined}
            preload="none"
            aria-label="Event audio evidence"
          />
          {audioError && <p className="error-text">{audioError}</p>}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void desk.run(
                'Review event',
                async () => {
                  const value = await desk.api.project<MonitoringEvent>(
                    desk.projectId,
                    'events/' + encodeURIComponent(event.id) + '/review',
                    { disposition, comment },
                  );
                  setEvent(value);
                },
                ['events'],
              );
            }}
          >
            <label>
              Disposition
              <select value={disposition} onChange={(e) => setDisposition(e.target.value)}>
                {['unreviewed', 'authorized', 'false_positive', 'investigate'].map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Operator comment
              <textarea
                maxLength={1000}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
              />
            </label>
            <div className="actions">
              <button disabled={desk.busy}>Save review</button>
              <button type="button" onClick={closeEvent}>
                Close event
              </button>
            </div>
          </form>
        </Modal>
      )}
      {original && (
        <Modal label="Original sweep" close={() => setOriginal(undefined)} wide>
          <div className="eyebrow">IMMUTABLE SOURCE</div>
          <h2>Original sweep</h2>
          <p>
            <strong>{original.levels.length} original points</strong> · {original.level_unit} · no
            client resampling
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
                  <th>Source index</th>
                  <th>Frequency · Hz</th>
                  <th>Level · {original.level_unit}</th>
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
              Previous points
            </button>
            <button
              disabled={originalOffset + 100 >= original.levels.length}
              onClick={() => setOriginalOffset((x) => x + 100)}
            >
              Next points
            </button>
            <button onClick={downloadOriginal}>Download original JSON</button>
            <button onClick={() => setOriginal(undefined)}>Close original</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
