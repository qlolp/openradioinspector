import { t, useLocale } from './i18n';
import { LocaleControls } from './LocaleControls';
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { Spectrum, Waterfall } from './charts';
import { originalJSON, encodePathSegment } from './wire';
import { Modal, Panel, hz, titleCase, utc } from './components';
import { Prepare } from './Prepare';
import { preparationReady } from './station';
import { BaselineCounts, sourceNames } from './DutySetup';
import {
  OriginalObservation,
  readObservation,
  ReceiverHold,
  ReceiverPanorama,
} from './ReceiverPanorama';
import { RfExplorerPass, type LocalHold } from './RfExplorerPass';
import { RfExplorer, RfExplorerSettings } from './RfExplorer';
import { RfExplorerResearch } from './RfExplorerResearch';
import { rfExplorerAvailable, rfExplorerReadback } from './rf-explorer';
import { Research } from './Research';
import { PairedRechecks } from './PairedRechecks';
import { AudioEvidence } from './AudioEvidence';
import { ScanPlan } from './ScanPlan';
import { coverageBands, scanChoices, scanKey } from './scan-plan';
import { prepareSweep } from './pipeline';
import {
  BandOverview,
  CoverageRow,
  EventQueue,
  EventReceiver,
  SignalHypotheses,
  Qualification,
  Recovery,
  StationStatus,
} from './StationPanels';
import { remember, save, useDesk, type Desk } from './useDesk';
import type { Connection } from './transport';
import type { MonitoringEvent, Observation, Page, Sweep } from './types';

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
              : preparationReady(desk.preflight)
                ? t('Collection available · duty checks need attention')
                : t('Preparation needs attention')
            : t('Checking core readiness')}
        </strong>
        <button
          className="text-button"
          disabled={desk.busy || !desk.project}
          onClick={() =>
            void desk.run('Run preflight', () => desk.refresh(['preflight', 'storage']), [])
          }
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
          {progress?.points != null || progress?.windows != null ? (
            <BaselineCounts progress={progress} />
          ) : progress?.bands ? (
            t('Confirm the band baselines in the scan plan')
          ) : (
            <>
              {progress?.sweeps ?? '—'}
              {' ' + t('sweeps ·') + ' '}
              {progress?.elapsed_seconds?.toFixed(1) ?? '—'}
              {' ' + t('s')}
            </>
          )}
        </span>
      </div>
      <p className="muted">
        {progress?.points != null || progress?.windows != null ? (
          t(
            'Collect at least 20 independent visits over 60 seconds in the ordinary environment before confirming the background.',
          )
        ) : (
          <>
            {t('Collect at least') + ' '}
            {progress?.minimum_sweeps || 20}
            {' ' + t('complete sweeps over')} {progress?.minimum_seconds || 60}
            {' ' + t('seconds in the ordinary environment.')}
          </>
        )}
      </p>
      <div className="actions">
        <button
          disabled={
            desk.busy ||
            !desk.project ||
            desk.guard?.active ||
            collecting ||
            !preparationReady(desk.preflight)
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
            desk.busy ||
            !desk.project ||
            desk.guard?.active ||
            !progress?.ready ||
            !confirm ||
            !!desk.project?.scan_plan
          }
        >
          {t('Confirm baseline')}
        </button>
      </form>
      {(!desk.project || !!desk.project.receiver_stream_id) && (
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
      )}
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
  localWindows = false,
  connectionLabel,
  readOnly = false,
}: {
  transport: Connection;
  readOnly?: boolean;
  onConnectionSettings?: () => void;
  localWindows?: boolean;
  connectionLabel?: string;
}) {
  useLocale();
  const [requestedWorkspace, setWorkspace] = useState(() => remember('workspace', 'monitoring'));
  const [rfSelection, setRfSelection] = useState(() => remember('rf-explorer-session', ''));
  const desk = useDesk(
    transport,
    readOnly || requestedWorkspace === 'rf-explorer' ? rfSelection : undefined,
    readOnly,
  );
  const workspace =
    requestedWorkspace === 'rf-explorer' && !desk.rfActive ? 'monitoring' : requestedWorkspace;
  const availableRf = rfExplorerAvailable(desk.capabilities?.features);
  const [theme, setTheme] = useState(() => remember('theme', 'system'));
  const [scanBand, setScanBand] = useState('');
  const [selectedEvent, setSelectedEvent] = useState<string>();
  const [wizard, setWizard] = useState<'new' | 'resume' | undefined>(),
    [sourceObservation, setSourceObservation] = useState<Observation>(),
    [rfHold, setRfHold] = useState<LocalHold>({ since: '', visible: false }),
    [event, setEvent] = useState<MonitoringEvent>(),
    [original, setOriginal] = useState<Sweep>();
  const [originalPoint, setOriginalPoint] = useState<{ id: string; index: number }>();
  const [originalOffset, setOriginalOffset] = useState(0),
    [audioUrl, setAudioUrl] = useState(''),
    [audioError, setAudioError] = useState('');
  const [disposition, setDisposition] = useState('unreviewed'),
    [comment, setComment] = useState('');
  const audio = useRef<HTMLAudioElement>(null),
    dialogGeneration = useRef(0),
    dialogAbort = useRef(new AbortController());
  const p = desk.project;
  const receiverOnly = p?.primary_source_kind === 'ardv1',
    rfPrimary = p?.primary_source_kind === 'rf_explorer';
  const scanBands = scanChoices(desk.live.rows, p);
  const selectedBand = scanBands.some((b) => b.key === scanBand)
    ? scanBand
    : (scanBands[0]?.key ?? '');
  const displayRows = useMemo(
    () =>
      selectedBand ? desk.live.rows.filter((row) => scanKey(row) === selectedBand) : desk.live.rows,
    [selectedBand, desk.live.rows],
  );
  const planBand = p?.scan_plan?.config.bands.find(
    (band) => JSON.stringify([p.scan_plan!.id, band.id]) === selectedBand,
  );
  const bandRange =
    planBand?.step_hz === 10000
      ? { low: Number(planBand.start_hz), high: Number(planBand.stop_hz) }
      : undefined;
  const originalTable = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const table = originalTable.current;
    const selected = table?.querySelector('tr[aria-current="true"]');
    if (table && selected)
      table.scrollTop +=
        selected.getBoundingClientRect().top -
        table.getBoundingClientRect().top -
        table.clientHeight / 2;
  }, [original, originalPoint, originalOffset]);
  const originalPlot = useMemo(() => {
    if (!original) return;
    try {
      return prepareSweep(original).sweep;
    } catch {
      return undefined;
    }
  }, [original]);
  const emptyBandReceipt = useMemo(
    () => ({ epoch: {}, total: 0n }),
    [selectedBand, desk.live.presentation.epoch],
  );
  const presentation = selectedBand
    ? (desk.live.scanPresentations?.get(selectedBand) ?? emptyBandReceipt)
    : desk.live.presentation;
  const unavailable = desk.live.unsupported,
    latest = unavailable ? undefined : displayRows.at(-1),
    receiver = desk.live.observation;
  const receiverWorker = desk.guard?.workers.find(
    (worker) => worker.stream_id === p?.receiver_stream_id,
  );
  const analyzerWorker = desk.guard?.workers.find(
    (worker) => worker.stream_id === p?.analyzer_stream_id,
  );
  const receiverSettings = receiverWorker?.current;
  const actualFrequency = receiverSettings?.observed_frequency_hz;
  const requestedFrequency = receiverSettings?.requested_frequency_hz;
  const coverageBand = coverageBands(analyzerWorker).find((band) => band.id === planBand?.id);
  const survey = rfPrimary ? latest?.rf_explorer?.survey : undefined;
  const fullWaterfallRange = survey ? { low: survey.start_hz, high: survey.stop_hz } : bandRange;
  const waterfallRange =
    fullWaterfallRange ??
    (latest && {
      low: latest.frequencies_hz[0],
      high: latest.frequencies_hz.at(-1)!,
    });
  const waterfall = (
    <>
      <div className="waterfall-title">
        <h3>{t('Waterfall')}</h3>
        {waterfallRange && (
          <span>
            {hz(waterfallRange.low)} – {hz(waterfallRange.high)}
          </span>
        )}
      </div>
      <Waterfall key={selectedBand} rows={displayRows} band={fullWaterfallRange} />
    </>
  );
  const setScreen = (value: string) => {
    if (readOnly) return;
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
    setSelectedEvent(undefined);
    setSourceObservation(undefined);
    setRfHold({ since: '', visible: false });
    setOriginal(undefined);
    setAudioUrl('');
    dialogGeneration.current++;
    dialogAbort.current.abort();
    dialogAbort.current = new AbortController();
  }, [desk.projectId, desk.connectionVersion, desk.rfActive, desk.rfActive && desk.rfSession?.id]);
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
    setSelectedEvent(summary.id);
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
    const obs = event.peak_source?.observation_id ? event.peak_source : source;
    if (obs?.observation_id) {
      const id = obs.observation_id,
        generation = dialogGeneration.current,
        signal = dialogAbort.current.signal;
      await desk.run(
        'Original observation',
        async () => {
          const original = await readObservation(
            desk.api,
            obs.session_id,
            obs.stream_id,
            id,
            signal,
          );
          if (!signal.aborted && generation === dialogGeneration.current)
            setSourceObservation(original);
        },
        [],
        signal,
      );
      return;
    }
    const sweepId = event.peak_source?.sweep_id || source?.sweep_id;
    if (sweepId) {
      await openOriginal(sweepId);
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
  const originalDialog = original && (
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
      <p>
        <Qualification
          label="Source qualification"
          value={original.settings?.measurement_qualification}
        />
      </p>
      {(desk.rfActive || rfPrimary) && (
        <RfExplorerSettings
          readback={rfExplorerReadback(original.settings)}
          frequencies={original.frequencies_hz}
          showPassId
        />
      )}
      {originalPlot ? (
        <div className="evidence-spectrum">
          <h3>{t('Source panorama')}</h3>
          <Spectrum sweep={originalPlot} />
        </div>
      ) : (
        <p className="muted">{t('Source frequency outside supported display range')}</p>
      )}
      {(desk.rfActive || rfPrimary) && !readOnly && (
        <RfExplorerResearch
          key={original.id}
          api={desk.api}
          source={original}
          features={desk.capabilities?.features ?? {}}
        />
      )}
      <div className="original-table" ref={originalTable}>
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
              <tr
                key={i}
                aria-current={
                  originalPoint?.id === original.id && originalPoint.index === originalOffset + i
                    ? true
                    : undefined
                }
              >
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
  );
  const actionStatus = (
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
  );
  const coverage = p?.coverage || 'stopped';
  const dutyStatus = p?.duty?.status;
  const waitingDuty = p?.duty?.enabled && dutyStatus === 'waiting';
  const guardLabel = waitingDuty
    ? t('Waiting for scheduled window')
    : dutyStatus === 'preflight_failed'
      ? t('Duty preflight failed')
      : dutyStatus &&
          ['stop_failed', 'coverage_lost', 'sleep', 'sleep_gap', 'manual_hold'].includes(dutyStatus)
        ? t(dutyStatus)
        : titleCase(p?.state || t('unavailable'));
  const header = (
    <>
      <a className="skip-link" href="#main">
        {t('Skip to workspace')}
      </a>
      <header className="station-header">
        <div className="topbar">
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
            </span>
          </a>
          <div className="topbar-right">
            <LocaleControls />
            <span className="node-pill">
              <span className={'dot ' + (desk.health ? 'ok' : 'warn')} />
              {connectionLabel && (
                <span className="node-address" title={connectionLabel}>
                  {connectionLabel}
                </span>
              )}
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
        </div>
        <div
          className="workspace-bar"
          title={desk.rfActive ? undefined : t('Local view · global guard state')}
        >
          <nav aria-label={t('Workspace')}>
            {!readOnly && (
              <>
                <button
                  aria-keyshortcuts="Alt+M"
                  aria-pressed={workspace === 'monitoring'}
                  onClick={() => setScreen('monitoring')}
                >
                  {t('Monitoring')}
                </button>
                <button
                  aria-keyshortcuts="Alt+R"
                  aria-pressed={workspace === 'research'}
                  onClick={() => setScreen('research')}
                >
                  {t('Research')}
                </button>
              </>
            )}
            {availableRf && (
              <button aria-pressed={desk.rfActive} onClick={() => setScreen('rf-explorer')}>
                RF Explorer
              </button>
            )}
          </nav>
          {!desk.rfActive && (
            <span className="workspace-note">{t('Local view · global guard state')}</span>
          )}
          <span className="shortcut">{t('Alt M / Alt R')}</span>
        </div>
      </header>
    </>
  );
  if (readOnly && !desk.rfActive)
    return (
      <div className="app-shell">
        {header}
        <main id="main">
          <h1 data-testid="workspace-heading">{t('Archived RF Explorer measurements')}</h1>
          <p className="muted">
            {desk.capabilities ? t('RF Explorer archive unavailable') : t('Connecting')}
          </p>
        </main>
      </div>
    );
  if (desk.rfActive)
    return (
      <div className="app-shell">
        {header}
        <RfExplorer
          key={desk.connectionVersion}
          readOnly={readOnly}
          desk={desk}
          actionStatus={actionStatus}
          select={(id) => {
            setRfSelection(id);
            save('rf-explorer-session', id);
          }}
          openOriginal={(sweep, index) => {
            setOriginalPoint(index === undefined ? undefined : { id: sweep.id, index });
            setOriginalOffset(index === undefined ? 0 : Math.floor(index / 100) * 100);
            setOriginal(sweep);
          }}
        />
        {originalDialog}
      </div>
    );
  return (
    <div className="app-shell">
      {header}
      <main id="main">
        <div className="station-context">
          <div className="page-heading">
            <div>
              <h1 data-testid="workspace-heading">{t('Spectrum desk')}</h1>
            </div>
            <button
              className="primary"
              onClick={() => setWizard('new')}
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
              <strong data-testid="guard-state">{guardLabel}</strong>
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
              {p?.duty?.last_error ||
                desk.guard?.error ||
                p?.last_error ||
                (waitingDuty
                  ? t('The node will run preflight when the scheduled window opens.')
                  : desk.guard?.active
                    ? t(
                        p?.primary_source_kind
                          ? 'The core owns the selected instruments'
                          : 'Core owns the receive pair',
                      )
                    : t('Guard is stopped · viewing does not arm instruments'))}
            </small>
          </div>
          <div className="actions">
            <a
              className="preparation-link"
              href="#receive-preparation"
              onClick={() => setScreen('monitoring')}
            >
              {t('Preparation checks')}
            </a>
            {p && desk.capabilities?.features.single_source_duty === true && (
              <button disabled={desk.busy} onClick={() => setWizard('resume')}>
                {t('Duty setup')}
              </button>
            )}
            <button
              className="primary"
              disabled={
                desk.busy ||
                !p ||
                desk.guard?.active ||
                p.duty?.enabled ||
                !desk.preflight?.ready ||
                !desk.preflight?.baseline_ready
              }
              onClick={() => void desk.command('arm')}
            >
              {t('Arm guard')}
            </button>
            <button
              className="danger"
              disabled={desk.busy || (!desk.guard?.active && !p?.duty?.enabled)}
              onClick={() => void desk.command('disarm')}
            >
              {t(p?.duty?.enabled && !desk.guard?.active ? 'Stop duty' : 'Disarm guard')}
            </button>
          </div>
        </div>
        {actionStatus}
        <Recovery desk={desk} />
        {!receiverOnly && (
          <section hidden={workspace !== 'research'} aria-label={t('Research workspace')}>
            <Research
              active={workspace === 'research'}
              pairedEvent={selectedEvent || ''}
              setPairedEvent={setSelectedEvent}
              desk={desk}
              openOriginal={(id) => {
                void openOriginal(id);
              }}
            />
          </section>
        )}
        <div className="live-grid">
          {receiverOnly ? (
            <ReceiverPanorama
              key={JSON.stringify([
                desk.connectionVersion,
                p?.active_session_id,
                p?.primary_stream_id,
              ])}
              desk={desk}
            />
          ) : (
            <Panel
              title={t('Live spectrum')}
              eyebrow={
                p?.primary_source_kind ? sourceNames[p.primary_source_kind] : t('tinySA Ultra')
              }
              className="spectrum-panel"
              aside={
                <span className="live-badge" data-testid="live-state">
                  <span className={'dot ' + (desk.live.state.startsWith('Live') ? 'ok' : 'warn')} />
                  {t(desk.live.state)}
                </span>
              }
            >
              {rfPrimary &&
                p?.primary_stream_id &&
                desk.capabilities?.features.rf_explorer_survey_panorama === true &&
                p.profile?.analyzer?.settings.survey_step_hz === 10000 && (
                  <RfExplorerPass
                    key={JSON.stringify([
                      desk.connectionVersion,
                      p.active_session_id,
                      p.primary_stream_id,
                    ])}
                    api={desk.api}
                    session={p.active_session_id}
                    stream={p.primary_stream_id}
                    running={!!desk.guard?.active || p.state === 'collecting'}
                    paused={false}
                    bookmarks={desk.capabilities.features.frequency_bookmarks === true}
                    readOnly={readOnly}
                    hold={rfHold}
                    setHold={setRfHold}
                    openOriginal={(s, i) => {
                      setOriginal(s);
                      setOriginalOffset(i == null ? 0 : Math.floor(i / 100) * 100);
                      setOriginalPoint(i == null ? undefined : { id: s.id, index: i });
                    }}
                  />
                )}
              {rfPrimary && latest?.rf_explorer && (
                <RfExplorerSettings
                  readback={latest.rf_explorer}
                  frequencies={latest.frequencies_hz}
                />
              )}
              <p className="muted">{t('Latest saved measurement')}</p>
              <div
                className="spectrum-metrics"
                role="group"
                aria-label={t('Latest saved measurement')}
              >
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
                <>
                  {scanBands.length > 0 && (
                    <label className="displayed-band">
                      {t('Displayed band')}
                      <select
                        value={selectedBand}
                        onChange={(event) => setScanBand(event.target.value)}
                      >
                        {scanBands.map((band) => (
                          <option key={band.key} value={band.key}>
                            {band.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {bandRange && (
                    <>
                      <BandOverview
                        key={selectedBand}
                        band={
                          desk.live.panorama?.id === p?.scan_plan?.id &&
                          desk.live.panorama?.revision === p?.scan_plan?.revision &&
                          planBand
                            ? desk.live.panorama?.bands.get(planBand.id)
                            : undefined
                        }
                        range={bandRange}
                        openOriginal={(id) => void openOriginal(id)}
                        busy={desk.busy}
                      />
                      {waterfall}
                      {coverageBand ? (
                        <CoverageRow band={coverageBand} />
                      ) : (
                        <Qualification label="Band qualification" />
                      )}
                      <h3>{t('Latest hardware window')}</h3>
                      {latest && (
                        <p className="muted">
                          {latest.scan_context?.window_index == null
                            ? '—'
                            : latest.scan_context.window_index + 1}{' '}
                          / {latest.scan_context?.window_count ?? '—'} ·{' '}
                          {hz(latest.frequencies_hz[0])} – {hz(latest.frequencies_hz.at(-1))}
                        </p>
                      )}
                    </>
                  )}
                  <Spectrum
                    sweep={latest}
                    liveSources={{ rows: displayRows, presentation }}
                    belowPlot={!bandRange && waterfall}
                  />
                </>
              )}
              <div
                className="chart-caption"
                role="group"
                aria-label={t('Latest saved measurement')}
              >
                <span>{t('Frequency · MHz')}</span>
                <span>
                  {unavailable?.point_count ?? latest?.original_point_count ?? 0}
                  {' ' + t('original points · display only')}
                </span>
              </div>
              {unavailable && waterfall}
              {!bandRange &&
                planBand &&
                (coverageBand ? (
                  <CoverageRow band={coverageBand} />
                ) : (
                  <Qualification label="Band qualification" />
                ))}
              <div
                className="source-footer"
                role="group"
                aria-label={t('Latest saved measurement')}
              >
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
          )}
          <div className="side-stack">
            {workspace === 'monitoring' && (
              <EventQueue
                desk={desk}
                selected={selectedEvent}
                open={(item) => void openEvent(item)}
              />
            )}
            {(!p || !!p.receiver_stream_id) && (
              <Panel
                title={t('AR-DV1')}
                eyebrow={t(receiverOnly ? 'PRIMARY INSTRUMENT' : 'INDEPENDENT RECEIVER')}
                aside={
                  <span className={'badge ' + (receiverWorker?.alive ? 'good' : '')}>
                    {receiverWorker?.alive
                      ? t('Receiving')
                      : titleCase(receiverWorker?.state || 'unavailable')}
                  </span>
                }
              >
                <small>{t('Last saved receiver frequency')}</small>
                <div className="receiver-frequency mono">{hz(receiver?.frequency_hz)}</div>
                {receiverSettings &&
                (requestedFrequency != null || receiverSettings.frequency_source) ? (
                  <dl className="status-list receiver-readback">
                    <div>
                      <dt>{t('Requested target')}</dt>
                      <dd>
                        {hz(
                          typeof requestedFrequency === 'number' ||
                            typeof requestedFrequency === 'bigint'
                            ? requestedFrequency
                            : undefined,
                        )}{' '}
                        · {String(receiverSettings.requested_mode ?? '—')}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('Receiver readback')}</dt>
                      <dd>
                        {receiverSettings.frequency_readback === true &&
                        (typeof actualFrequency === 'number' || typeof actualFrequency === 'bigint')
                          ? hz(actualFrequency) + ' · ' + (receiverSettings.observed_mode ?? '—')
                          : t('Unavailable')}
                      </dd>
                    </div>
                  </dl>
                ) : null}
                <div className="receiver-stats">
                  <div>
                    <small>{t('MODE')}</small>
                    <strong>{receiver?.mode || '—'}</strong>
                  </div>
                  <div>
                    <small>{t('RAW LEVEL')}</small>
                    <strong className="mono">
                      {receiver?.level ?? '—'}{' '}
                      <small>{receiver?.level_unit ? t(receiver.level_unit) : ''}</small>
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
                    <strong>
                      {desk.guard
                        ? t(desk.guard.receiver_busy ? 'In progress' : 'Idle')
                        : t('Unavailable')}
                    </strong>
                  </div>
                </div>
                <p className="muted">
                  {t('S-meter counts retain their original unit. No dBm conversion is applied.')}
                </p>
                <div className="device-footer">
                  <span className="mono">
                    {receiverWorker?.measurements ?? '—'}
                    {' ' + t('observations')}
                  </span>
                  <span>{receiverWorker?.adapter || t('No active worker')}</span>
                </div>
                {receiverWorker?.last_error && (
                  <p className="error-text">{receiverWorker.last_error}</p>
                )}
                {receiverOnly && (
                  <ReceiverHold
                    key={JSON.stringify([desk.connectionVersion, p.id, p.active_session_id])}
                    desk={desk}
                  />
                )}
              </Panel>
            )}
            <StationStatus desk={desk} analyzer={receiverOnly ? receiverWorker : analyzerWorker} />
          </div>
        </div>
        <section hidden={workspace !== 'monitoring'} aria-label={t('Monitoring workspace')}>
          <div className="preparation-grid" id="receive-preparation" tabIndex={-1}>
            <PreparationPanel desk={desk} />
            {!receiverOnly && !rfPrimary && <ScanPlan desk={desk} />}
            <AllowedPanel desk={desk} />
          </div>
        </section>
        <footer className="page-footer">
          <span>{t('OpenRadioInspector · v1 contract · receive only')}</span>
          <span>{t('UTC timestamps · integer Hz · original units')}</span>
        </footer>
      </main>
      {wizard && (
        <Prepare
          desk={desk}
          onLocalArchiveSettings={localWindows ? onConnectionSettings : undefined}
          resume={wizard === 'resume' ? p : undefined}
          close={() => setWizard(undefined)}
        />
      )}
      {event && (
        <Modal label={t('Event details')} close={closeEvent}>
          <h2>{t('Event details')}</h2>
          <div className="event-frequency mono">{hz(event.center_frequency_hz)}</div>
          <p>
            {titleCase(event.kind)} · {event.peak_level}
            {' ' + t(event.level_unit ?? 'dBm') + ' · '}
            {event.source_count}
            {' ' +
              t(event.level_unit === 's_meter_count' ? 'source observations' : 'source sweeps')}
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
            <button
              className="primary"
              disabled={
                desk.busy ||
                (event.level_unit === 's_meter_count' &&
                  !event.peak_source?.observation_id &&
                  !event.source_refs?.some((s) => s.observation_id))
              }
              onClick={() => void eventOriginal()}
            >
              {t(event.level_unit === 's_meter_count' ? 'Original observation' : 'Original sweep')}
            </button>
            <button disabled={desk.busy || !event.analysis?.audio} onClick={() => void playAudio()}>
              {t('Play audio')}
            </button>
          </div>
          <EventReceiver event={event} api={desk.api} />
          {desk.capabilities?.features.monitoring_signal_catalog === true && (
            <SignalHypotheses api={desk.api} projectId={desk.projectId} event={event} />
          )}
          {!!p?.analyzer_stream_id &&
            !!p.receiver_stream_id &&
            desk.capabilities?.features.paired_event_rechecks === true && (
              <PairedRechecks
                key={JSON.stringify([desk.connectionVersion, desk.projectId, event.id])}
                desk={desk}
                eventId={event.id}
              />
            )}
          <audio
            ref={audio}
            controls
            src={audioUrl || undefined}
            preload="none"
            aria-label={t('Event audio evidence')}
          />
          {audioError && <p className="error-text">{t(audioError)}</p>}
          {event.analysis?.audio && (
            <AudioEvidence
              key={desk.connectionVersion + '\0' + desk.projectId + '\0' + event.id}
              api={desk.api}
              projectId={desk.projectId}
              eventId={event.id}
              available={desk.capabilities?.features.audio_evidence_export === true}
              audio={transport.audio}
            />
          )}
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
      {sourceObservation && (
        <OriginalObservation
          source={sourceObservation}
          close={() => setSourceObservation(undefined)}
        />
      )}
      {originalDialog}
    </div>
  );
}
