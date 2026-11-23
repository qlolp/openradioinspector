import { useCallback, useRef, useState } from 'react';
import {
  receiveSession,
  receiverGridCount,
  tinyDutyPlan,
  type DutySource,
  type ReceiveForm,
} from './api';
import { hz, Modal, utc } from './components';
import { boardProfiles, rfExplorerReadback, surveyCounts } from './rf-explorer';
import { t } from './i18n';
import { formatBytes, formatQuantity, measuredRate, preparationReady } from './station';
import { useArchiveRead } from './RfExplorerOriginals';
import { encodePathSegment, originalJSON, originalObservationJSON } from './wire';
import { message, type Desk } from './useDesk';
import type { Project, Session, Sweep, Observation, Preflight } from './types';
import { DutyPolicy, ArchiveForecast } from './DutyPolicy';

export const sourceNames = {
  tinysa: 'tinySA Ultra',
  rf_explorer: 'RF Explorer 3G Combo',
  ardv1: 'AOR AR-DV1',
};
const firstRanges: Record<DutySource, [string, string]> = {
  tinysa: ['88', '108'],
  rf_explorer: ['250', '251.11'],
  ardv1: ['100', '100.2'],
};
function FirstOriginal({ row, api }: { row: Observation | Sweep; api: Desk['api'] }) {
  const [open, setOpen] = useState(false);
  const receiver = 'frequency_hz' in row;
  const path =
    '/sessions/' +
    encodePathSegment(row.session_id) +
    '/' +
    (receiver ? 'observations' : 'sweeps') +
    '/' +
    encodePathSegment(row.id);
  const original = useArchiveRead<Observation | Sweep>(api, open ? path : undefined);
  const saved = original?.value;
  const matches =
    saved &&
    saved.session_id === row.session_id &&
    saved.stream_id === row.stream_id &&
    saved.id === row.id &&
    saved.level_unit === row.level_unit;
  const frequency = receiver ? row.frequency_hz : row.frequencies_hz[0];
  const level = receiver ? row.level : row.levels[0];
  const metadata = receiver ? row.metadata : row.settings;
  const readback = !receiver ? rfExplorerReadback(row.settings) : undefined;
  const unit = row.level_unit === 's_meter_count' ? t('S-meter counts') : row.level_unit;
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        {t('Open archived original')} · {receiver ? t('Receiver reading') : t('First sweep bin')} ·{' '}
        {hz(frequency)} · {level == null ? t('Unavailable') : String(level) + ' ' + unit}
      </summary>
      <p>
        {metadata?.simulated === true && t('Training data')}
        {typeof metadata?.adapter === 'string' && <> · {metadata.adapter}</>}
        {readback && (
          <>
            {' '}
            ·{' '}
            {readback.active_board
              ? t(boardProfiles[readback.active_board].label)
              : t('SMA readback unavailable')}{' '}
            · {readback.detector ?? t('Detector unavailable')}
          </>
        )}
      </p>
      <p>
        {t('Session')} <code>{row.session_id}</code> · {t('Stream')} <code>{row.stream_id}</code>
      </p>
      {open && (
        <>
          <code>/api/v1{path}</code>
          {original?.error && <p role="alert">{t(original.error)}</p>}
          {saved &&
            (matches ? (
              <pre className="raw-observation">
                {'frequency_hz' in saved ? originalObservationJSON(saved) : originalJSON(saved)}
              </pre>
            ) : (
              <p role="alert">{t('Source evidence mismatch')}</p>
            ))}
        </>
      )}
    </details>
  );
}
const reasons = {
  tinysa:
    'The tinySA measures spectrum sweeps in dBm. The 10 kHz grid uses successive hardware windows. The core checks the installed receive profile.',
  rf_explorer:
    'RF Explorer uses the active SMA input and Normal detector. A 10 kHz survey covers the range in 112-point windows and saves dBm sweeps.',
  ardv1:
    'AR-DV1 visits each frequency in FM. Lower raw S-meter counts mean a stronger reading. Inspecting one frequency pauses this receiver’s broad coverage.',
};
function sourceOf(session?: Session): DutySource | undefined {
  if (!session) return;
  const analyzer = session.streams.filter((s) => s.role === 'spectrum_analyzer'),
    receiver = session.streams.filter((s) => s.role === 'receiver');
  if (
    session.streams.length !== analyzer.length + receiver.length ||
    analyzer.length > 1 ||
    receiver.length > 1
  )
    return;
  if (
    receiver.length &&
    !['ardv1-serial', 'ardv1-sim'].includes(String(receiver[0].settings?.adapter))
  )
    return;
  if (!analyzer.length)
    return receiver.length &&
      (receiver[0].settings?.fast_scan as { cyclic?: boolean })?.cyclic === true
      ? 'ardv1'
      : undefined;
  if (analyzer[0].settings?.adapter === 'rf-explorer-3g-serial') return 'rf_explorer';
  if (['tinysa-sim', 'tinysa-serial'].includes(String(analyzer[0].settings?.adapter)))
    return 'tinysa';
}
export function BaselineCounts({ progress }: { progress?: Preflight['baseline_collection'] }) {
  if (!progress) return <>{t('Awaiting archive measurements')}</>;
  if (progress.bands)
    return (
      <>
        {progress.bands.map((b) => (
          <span key={b.band_id}>
            {b.name} · {b.ready_windows ?? Number(b.ready)} / {b.eligible_windows ?? 1}{' '}
            {t('ready windows')}{' '}
          </span>
        ))}
      </>
    );
  return (
    <>
      {progress.points != null
        ? t('{ready} / {total} frequencies ready', {
            ready: formatQuantity(progress.ready_points ?? 0),
            total: formatQuantity(progress.points),
          })
        : progress.windows != null
          ? t('{ready} / {total} windows ready', {
              ready: formatQuantity(progress.ready_windows ?? 0),
              total: formatQuantity(progress.windows),
            })
          : t('{count} sweeps', { count: progress.sweeps ?? 0 })}
      {progress.passes != null && <> · {t('{count} passes', { count: progress.passes })}</>}
      {' · '}
      {formatQuantity(progress.elapsed_seconds ?? 0)} / {progress.minimum_seconds ?? 60} {t('s')}
    </>
  );
}
export interface PathChecks {
  notes: string;
  receive: boolean;
  location: boolean;
}
export function ReceiveChecks({
  value,
  change,
}: {
  value: PathChecks;
  change: (key: keyof PathChecks, v: string | boolean) => void;
}) {
  return (
    <>
      <label>
        {t('Antenna and receive path notes')}
        <textarea
          rows={2}
          required
          maxLength={1000}
          value={value.notes}
          onChange={(e) => change('notes', e.target.value)}
        />
      </label>
      <label className="check">
        <input
          type="checkbox"
          required
          checked={value.receive}
          onChange={(e) => change('receive', e.target.checked)}
        />
        {t('Receive path and antenna checked')}
      </label>
      <label className="check">
        <input
          type="checkbox"
          required
          checked={value.location}
          onChange={(e) => change('location', e.target.checked)}
        />
        {t('Location and ordinary RF environment checked')}
      </label>
    </>
  );
}
function hzInput(value: string, decimals: number) {
  if (!new RegExp('^\\d+(?:[.,]\\d{1,' + decimals + '})?$').test(value)) return NaN;
  const [whole, fraction = ''] = value.replace(',', '.').split('.');
  return Number(whole + fraction.padEnd(decimals, '0'));
}
type Archive = {
  storage_id: string;
  label: string;
  database: string;
  available_bytes: number | null;
  relocation: string;
};
const headings = [
  'Choose instruments',
  'Range and spacing',
  'Archive and first measurement',
  'Record the background',
  'Notifications and duty',
];
function DraftPorts({
  project,
  desk,
  checks,
  onSaved,
}: {
  project: Project;
  desk: Desk;
  checks: PathChecks;
  onSaved: (p: Project) => void;
}) {
  const [edits, setEdits] = useState<Partial<Record<'analyzer' | 'receiver', string>>>({});
  const roles = (['analyzer', 'receiver'] as const).filter((role) =>
    ['tinysa-serial', 'ardv1-serial', 'rf-explorer-3g-serial'].includes(
      String(project.profile?.[role]?.settings.adapter),
    ),
  );
  const corrections = Object.fromEntries(
    roles.flatMap((role) => {
      const from = project.profile![role]!.port,
        to = edits[role]?.trim();
      return to && to !== from ? [[role, { from, to }]] : [];
    }),
  );
  if (!roles.length) return null;
  return (
    <details>
      <summary>{t('Correct the project ports')}</summary>
      {roles.map((role) => (
        <label key={role}>
          {t(role === 'receiver' ? 'AR-DV1 serial port' : 'Analyzer serial port')}
          <input
            list="duty-serial-ports"
            maxLength={1024}
            value={edits[role] ?? project.profile![role]!.port}
            onChange={(e) => setEdits({ ...edits, [role]: e.target.value })}
          />
        </label>
      ))}
      <button
        type="button"
        disabled={desk.busy || !Object.keys(corrections).length || !checks.notes.trim()}
        onClick={async () => {
          const fixed = await desk.run(
            'Save project ports',
            () =>
              desk.api.project<Project>(project.id, 'preparation', {
                antenna_notes: checks.notes,
                receive_path_confirmed: checks.receive,
                location_confirmed: checks.location,
                port_corrections: corrections,
              }),
            ['status', 'preflight'],
          );
          if (fixed) {
            setEdits({});
            onSaved(fixed);
            await desk.reloadSessions();
          }
        }}
      >
        {t('Save project ports')}
      </button>
    </details>
  );
}
export function DutySetup({
  desk,
  close,
  resume,
  onLocalArchiveSettings,
}: {
  desk: Desk;
  close: () => void;
  resume?: Project;
  onLocalArchiveSettings?: () => void;
}) {
  const [usingSaved, setUsingSaved] = useState(!!resume);
  const [step, setStep] = useState(resume ? (resume.baseline_id ? 4 : 2) : 0),
    [sessionId, setSessionId] = useState('');
  const [kind, setKind] = useState<DutySource>(resume?.primary_source_kind ?? 'tinysa'),
    [checker, setChecker] = useState(false),
    [simulate, setSimulate] = useState(false);
  const [name, setName] = useState(resume?.name ?? ''),
    [site, setSite] = useState(resume?.site ?? '');
  const [analyzerPort, setAnalyzerPort] = useState(''),
    [receiverPort, setReceiverPort] = useState('');
  const [low, setLow] = useState(firstRanges[kind][0]),
    [high, setHigh] = useState(firstRanges[kind][1]),
    [spacing, setSpacing] = useState('10');
  const [board, setBoard] = useState<keyof typeof boardProfiles>('expansion'),
    [storage, setStorage] = useState('');
  const [checks, setChecks] = useState<PathChecks>({
      notes: resume?.preparation?.antenna_notes ?? '',
      receive: !!resume?.preparation?.receive_path_confirmed,
      location: !!resume?.preparation?.location_confirmed,
    }),
    [confirm, setConfirm] = useState(false);
  const [prepared, setPrepared] = useState<Project | undefined>(resume),
    [starting, setStarting] = useState(false);
  const createdSession = useRef<Session | undefined>(undefined),
    createdProject = useRef<Project | undefined>(resume),
    planSaved = useRef(false);
  const features = desk.capabilities?.features ?? {};
  const existing = desk.sessions.find((s) => s.id === sessionId),
    selected = sourceOf(existing) ?? kind;
  const p = desk.project?.id === prepared?.id ? desk.project : prepared;
  const activeSession = p?.active_session_id,
    primary = p?.primary_stream_id || p?.analyzer_stream_id || p?.receiver_stream_id;
  const receiver = (p?.primary_source_kind ?? selected) === 'ardv1';
  const acct = p?.id === desk.project?.id ? desk.preflight : undefined;
  const form: ReceiveForm = {
    name,
    simulator: simulate,
    primarySource: kind,
    checkEvents: checker && kind !== 'ardv1',
    analyzerPort,
    receiverPort,
    startHz: hzInput(low, 6),
    stopHz: hzInput(high, 6),
    stepHz: hzInput(spacing, 3),
    points: 201,
    rbwHz: 10000,
    tinyGrid:
      kind === 'tinysa' && hzInput(spacing, 3) === 10_000 && features.scan_plan_grid_10khz === true,
  };
  const [portRevision, setPortRevision] = useState(0);
  const portList = useArchiveRead<{ ports: string[] }>(
    desk.api,
    (step === 0 || step === 2) && !simulate && features.serial_port_candidates === true
      ? '/serial-ports'
      : undefined,
    0,
    false,
    portRevision,
  );
  const archive = useArchiveRead<Archive>(
    desk.api,
    step === 2 && features.archive_location === true ? '/archive/location' : undefined,
  );
  const samplePath =
    prepared && step === 2 && activeSession && primary && p?.state === 'collecting'
      ? '/sessions/' +
        encodePathSegment(activeSession) +
        '/' +
        (receiver ? 'observations' : 'sweeps') +
        '?' +
        new URLSearchParams({ stream_id: primary, limit: '1' })
      : undefined;
  const isOriginal = useCallback(
    (row: Observation | Sweep) =>
      row.session_id === activeSession &&
      row.stream_id === primary &&
      (receiver
        ? 'frequency_hz' in row && row.level_unit === 's_meter_count' && Number.isFinite(row.level)
        : 'frequencies_hz' in row &&
          row.level_unit === 'dBm' &&
          row.frequencies_hz.length > 0 &&
          row.levels.length === row.frequencies_hz.length),
    [activeSession, primary, receiver],
  );
  const hasOriginal = useCallback(
    (rows: (Observation | Sweep)[]) => rows.some(isOriginal),
    [isOriginal],
  );
  const sampleRead = useArchiveRead<(Observation | Sweep)[]>(
    desk.api,
    samplePath,
    1800,
    false,
    0,
    hasOriginal,
  );
  const first = sampleRead?.value?.find(isOriginal);
  const [hoursEdit, setHours] = useState<string>();
  const [backupEdit, setBackup] = useState<boolean>();
  const [restoreEdit, setRestore] = useState<boolean>();
  const hours = hoursEdit ?? String((p?.archive?.minimum_retention_seconds ?? 43_200) / 3600);
  const plannedSeconds = Math.round(Number(hours.replace(',', '.')) * 3600);
  const validBudget =
    Number.isSafeInteger(plannedSeconds) && plannedSeconds >= 60 && plannedSeconds <= 31_536_000;
  const backup = backupEdit ?? p?.archive?.reserve_backup ?? true;
  const restore = restoreEdit ?? p?.archive?.reserve_restore ?? true;
  const w = desk.guard?.workers.find((worker) => worker.stream_id === primary),
    rate = measuredRate(w);
  let invalid = '',
    count: number | undefined,
    windows: number | undefined;
  if (!usingSaved && !existing && step <= 2) {
    try {
      if (kind === 'ardv1' && features.ardv1_cyclic_panorama !== true)
        throw Error(
          'Enable the core AR-DV1 cyclic receive profile or choose another primary instrument.',
        );
      if (kind === 'rf_explorer' && features.rf_explorer_10khz_survey !== true)
        throw Error(
          'This node does not advertise RF Explorer 10 kHz surveys. Check the installed core and serial profile.',
        );
      if (kind === 'tinysa' && !simulate && features.explicit_serial_adapters !== true)
        throw Error('This node does not advertise physical serial instruments.');
      const request = receiveSession(form);
      if (kind === 'ardv1') count = receiverGridCount(form.startHz, form.stopHz, form.stepHz!);
      else if (kind === 'tinysa') {
        count = form.tinyGrid
          ? tinyDutyPlan(form).bands[0].points
          : Number(request.streams[0].settings.points);
        windows = Math.ceil(count / 290);
      } else {
        const limits = boardProfiles[board];
        if (form.startHz < limits.low || form.stopHz > limits.high)
          throw Error(
            'The range exceeds this SMA input. Choose the other input or enter a range inside its limits.',
          );
        const survey = surveyCounts({ start_hz: form.startHz, stop_hz: form.stopHz });
        count = survey.points;
        windows = survey.windows;
      }
    } catch (cause) {
      invalid = message(cause);
    }
  }
  let suggestReceiver = false;
  if (invalid && kind !== 'ardv1' && features.ardv1_cyclic_panorama === true) {
    try {
      receiverGridCount(form.startHz, form.stopHz, form.stepHz!);
      suggestReceiver = true;
    } catch {
      /* No compatible receiver proposal. */
    }
  }
  function rangeFull() {
    const limits =
      selected === 'rf_explorer' ? boardProfiles[board] : { low: 100000, high: 1300000000 };
    setLow(String(limits.low / 1e6));
    setHigh(String(limits.high / 1e6));
  }
  async function start() {
    if (
      !storage ||
      storage !== archive?.value?.storage_id ||
      !checks.notes.trim() ||
      !checks.receive ||
      !checks.location ||
      (!usingSaved && !existing && invalid)
    )
      return;
    setStarting(true);
    await desk.run(
      'Start receive collection',
      async () => {
        let project = createdProject.current;
        if (!project) {
          const session =
            existing ??
            createdSession.current ??
            (await desk.api.post<Session>('/sessions', receiveSession(form)));
          createdSession.current = session;
          project = await desk.api.post<Project>('/monitoring-projects', {
            name,
            site,
            session_id: session.id,
          });
          createdProject.current = project;
          setPrepared(project);
        }
        if (!planSaved.current && !usingSaved && !existing && form.tinyGrid) {
          await desk.api.project(project.id, 'scan-plan', tinyDutyPlan(form));
          planSaved.current = true;
        }
        await desk.api.project(project.id, 'preparation', {
          antenna_notes: checks.notes,
          receive_path_confirmed: checks.receive,
          location_confirmed: checks.location,
        });
        await desk.loadProjects('', [], project);
        setPrepared(project);
        const preflight = await desk.api.project<Preflight>(project.id, 'preflight');
        if (!preparationReady(preflight))
          throw Error(
            preflight.checks
              .filter((c) => !c.ok)
              .map((c) => c.detail)
              .join(' · ') || 'Preparation needs attention',
          );
        const started = await desk.api.project<{ project: Project }>(
          project.id,
          'preparation/start',
          {},
        );
        project = started.project;
        setPrepared(project);
        createdProject.current = project;
        await desk.loadProjects('', [], project);
        await desk.reloadSessions();
      },
      ['status', 'preflight', 'storage'],
    );
    setStarting(false);
  }
  async function background() {
    if (
      !p ||
      !confirm ||
      !acct?.baseline_collection.ready ||
      (features.monitoring_storage_forecast === true && !validBudget)
    )
      return;
    const bands = acct.baseline_collection.bands?.filter(
      (b) => (b.eligible_windows ?? 1) > 0 && !b.baseline_id,
    );
    const result = await desk.run(
      'Confirm baseline',
      async () => {
        if (features.monitoring_storage_forecast === true) {
          await desk.api.project(p.id, 'settings', {
            archive: {
              minimum_retention_seconds: plannedSeconds,
              reserve_backup: backup,
              reserve_restore: restore,
            },
          });
        }
        for (const band of bands ?? [undefined])
          await desk.api.project(p.id, 'baselines', {
            name: t('Ordinary environment'),
            location: site || p.site,
            confirm: true,
            ...(band && { band_id: band.band_id }),
          });
        return true;
      },
      ['status', 'preflight', 'storage'],
    );
    if (result) setStep(4);
  }
  const visitsPerPass = receiver
    ? (count ?? acct?.baseline_collection?.points)
    : (windows ??
      acct?.baseline_collection?.windows ??
      (acct?.baseline_collection?.bands?.length
        ? acct.baseline_collection.bands.reduce(
            (total, band) => total + (band.eligible_windows ?? 1),
            0,
          )
        : 1));
  const progress = acct?.baseline_collection;
  const remainingPasses =
    progress?.minimum_passes != null && progress.passes != null
      ? Math.max(0, progress.minimum_passes - progress.passes)
      : undefined;
  const remainingSweeps =
    progress?.minimum_sweeps != null && progress.sweeps != null
      ? Math.max(0, progress.minimum_sweeps - progress.sweeps)
      : undefined;
  const remainingVisits =
    remainingPasses != null && visitsPerPass ? remainingPasses * visitsPerPass : remainingSweeps;
  const estimatedBackground =
    progress && rate && remainingVisits != null
      ? Math.ceil(
          Math.max(
            0,
            (progress.minimum_seconds ?? 60) - (progress.elapsed_seconds ?? 0),
            remainingVisits / rate,
          ),
        )
      : undefined;
  const locked = !!createdSession.current || !!createdProject.current;
  return (
    <Modal
      label={t('Prepare a receive-only project')}
      close={() => {
        if (!desk.busy) close();
      }}
      wide
    >
      <div className="eyebrow">
        {t('Prepare a receive-only project')} · {step + 1} / 5
      </div>
      <h2>{t(headings[step])}</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (step === 0 && (!name.trim() || !site.trim())) return;
          if (step === 1 && !existing && invalid) return;
          if (step <= 1) setStep(step + 1);
          if (step === 2) {
            if (first) {
              setStep(3);
            } else void start();
          }
          if (step === 3) {
            if (acct?.baseline_ready) setStep(4);
            else void background();
          }
        }}
      >
        {features.serial_port_candidates === true && !simulate && (step === 0 || step === 2) && (
          <datalist id="duty-serial-ports">
            {portList?.value?.ports.map((port) => (
              <option key={port} value={port} />
            ))}
          </datalist>
        )}
        {step === 0 && (
          <>
            <div className="form-grid">
              <label>
                {t('Project name')}
                <input
                  required
                  maxLength={200}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label>
                {t('Site or location')}
                <input
                  required
                  maxLength={200}
                  value={site}
                  onChange={(e) => setSite(e.target.value)}
                />
              </label>
            </div>
            <label>
              {t('Receive session')}
              <select value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
                <option value="">{t('Create a new receive session')}</option>
                {desk.sessions
                  .filter((s) => sourceOf(s))
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} · {s.state}
                    </option>
                  ))}
              </select>
            </label>
            {!existing && (
              <>
                <label>
                  {t('Primary instrument')}
                  <select
                    value={kind}
                    onChange={(e) => {
                      const source = e.target.value as DutySource;
                      setKind(source);
                      setLow(firstRanges[source][0]);
                      setHigh(firstRanges[source][1]);
                      setSpacing('10');
                      setSimulate(false);
                    }}
                  >
                    {Object.entries(sourceNames).map(([key, label]) => (
                      <option value={key} key={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="callout">{t(reasons[kind])}</p>
                {kind !== 'ardv1' && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={checker}
                      onChange={(e) => setChecker(e.target.checked)}
                    />
                    {t('Add an independent AR-DV1 for event checks')}
                  </label>
                )}
                {kind === 'rf_explorer' && checker && (
                  <p>
                    {t(
                      'AR-DV1 can check events. Comparing paired archived visits requires tinySA and AR-DV1.',
                    )}
                  </p>
                )}
                {kind === 'tinysa' && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={simulate}
                      onChange={(e) => setSimulate(e.target.checked)}
                    />
                    {t('Use explicit simulators')}
                  </label>
                )}
                {!simulate && features.serial_port_candidates === true && (
                  <div>
                    <button type="button" onClick={() => setPortRevision((n) => n + 1)}>
                      {t('Refresh node ports')}
                    </button>
                    {portList?.error && <p role="alert">{portList.error}</p>}
                    {portList?.value?.ports.length === 0 && (
                      <p>
                        {t(
                          'The node listed no serial ports. Enter a port or connect the instrument and refresh.',
                        )}
                      </p>
                    )}
                  </div>
                )}
                {!simulate && (
                  <div className="form-grid">
                    {kind !== 'ardv1' && (
                      <label>
                        {t(kind === 'tinysa' ? 'tinySA serial port' : 'RF Explorer serial port')}
                        <input
                          required
                          list="duty-serial-ports"
                          value={analyzerPort}
                          onChange={(e) => setAnalyzerPort(e.target.value)}
                          placeholder={t('/dev/ttyACM0 or COM3')}
                        />
                      </label>
                    )}
                    {(kind === 'ardv1' || checker) && (
                      <label>
                        {t('AR-DV1 serial port')}
                        <input
                          required
                          list="duty-serial-ports"
                          value={receiverPort}
                          onChange={(e) => setReceiverPort(e.target.value)}
                          placeholder={t('/dev/ttyUSB0 or COM4')}
                        />
                      </label>
                    )}
                  </div>
                )}
              </>
            )}
          </>
        )}
        {step === 1 &&
          (existing ? (
            <>
              <h3>{existing.name}</h3>
              <p>
                {sourceNames[selected]} · {t('Existing core session')}
              </p>
              <pre className="raw-observation">
                {JSON.stringify(
                  existing.streams.map((s) => ({
                    device: s.device,
                    port: s.port,
                    settings: s.settings,
                  })),
                  (_, v) => (typeof v === 'bigint' ? v.toString() : v),
                  2,
                )}
              </pre>
            </>
          ) : (
            <>
              <p>
                {sourceNames[kind]} · {t(reasons[kind])}
              </p>
              {kind === 'rf_explorer' && (
                <label>
                  {t('Intended SMA input')}
                  <select value={board} onChange={(e) => setBoard(e.target.value as typeof board)}>
                    {Object.entries(boardProfiles).map(([id, b]) => (
                      <option key={id} value={id}>
                        {t(b.label)} · {b.low / 1e6}–{b.high / 1e6} MHz
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="actions">
                <button
                  type="button"
                  onClick={() => {
                    setLow(firstRanges[kind][0]);
                    setHigh(firstRanges[kind][1]);
                    setSpacing('10');
                  }}
                >
                  {t('Short first range')}
                </button>
                <button type="button" onClick={rangeFull}>
                  {t('Full supported range')}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setLow('100');
                    setHigh('400');
                  }}
                >
                  100–400 MHz
                </button>
              </div>
              <div className="form-grid">
                <label>
                  {t('Start (MHz)')}
                  <input
                    required
                    inputMode="decimal"
                    value={low}
                    onChange={(e) => setLow(e.target.value)}
                  />
                </label>
                <label>
                  {t('Stop (MHz)')}
                  <input
                    required
                    inputMode="decimal"
                    value={high}
                    onChange={(e) => setHigh(e.target.value)}
                  />
                </label>
                <label>
                  {t('Step (kHz)')}
                  <input
                    required
                    inputMode="decimal"
                    value={spacing}
                    onChange={(e) => setSpacing(e.target.value)}
                  />
                </label>
              </div>
              {count && (
                <div className="baseline-status">
                  <span>{t('Frequencies per pass')}</span>
                  <strong>{formatQuantity(count)}</strong>
                  {windows && (
                    <span>{t('{count} hardware windows', { count: formatQuantity(windows) })}</span>
                  )}
                </div>
              )}
              {kind === 'ardv1' && (
                <p>
                  {t(
                    'The stop frequency is included even when the last step is shorter. The receiver repeats the plan until stopped.',
                  )}
                </p>
              )}
              {kind === 'tinysa' && form.stopHz > 800000000 && (
                <p className="callout">
                  {t(
                    'Above 800 MHz the ZS405 grid is observation-only. The core qualifies windows individually for automatic baselines.',
                  )}
                </p>
              )}
              {invalid && (
                <p role="alert" className="error-text">
                  {t(invalid)}{' '}
                  {suggestReceiver &&
                    t(
                      'AR-DV1 can visit this range at the requested spacing in raw S-meter counts. Select it in the instrument step to use that plan.',
                    )}
                </p>
              )}
            </>
          ))}
        {step === 2 && (
          <>
            <p>
              {name} · {site} · {sourceNames[selected]}
            </p>
            <label>
              {t('Archive on the recording node')}
              <select
                required
                disabled={starting || p?.state === 'collecting'}
                value={p?.state === 'collecting' ? (archive?.value?.storage_id ?? '') : storage}
                onChange={(e) => setStorage(e.target.value)}
              >
                <option value="">{t('Choose the node archive')}</option>
                {archive?.value && (
                  <option value={archive.value.storage_id}>
                    {archive.value.label} · {archive.value.database}
                  </option>
                )}
              </select>
            </label>
            <p className="muted">
              {t('Free on archive disk')} · {formatBytes(archive?.value?.available_bytes)}.{' '}
              {t(
                onLocalArchiveSettings
                  ? 'To choose another drive on this PC, stop the local node in Connection settings and choose the archive folder before restarting.'
                  : 'Change the archive location in the local node supervisor.',
              )}
            </p>
            {onLocalArchiveSettings && (
              <button type="button" disabled={desk.busy} onClick={onLocalArchiveSettings}>
                {t('Leave setup for local archive settings')}
              </button>
            )}
            {features.archive_location !== true && (
              <p role="alert">
                {t(
                  'This node does not publish its archive location. Use a core with node archive selection.',
                )}
              </p>
            )}
            {archive?.error && <p role="alert">{t(archive.error)}</p>}
            <ReceiveChecks value={checks} change={(k, v) => setChecks((c) => ({ ...c, [k]: v }))} />
            {first ? (
              <>
                <p className="callout">
                  {t('Verified archived measurement')} · <code>{first.id}</code>
                  <br />
                  {utc(first.interval.ended_at)}
                </p>
                <FirstOriginal key={first.id} row={first} api={desk.api} />
                <p>
                  {t('Estimated pass duration')} ·{' '}
                  {rate && visitsPerPass
                    ? formatQuantity(visitsPerPass / rate) + ' ' + t('s')
                    : t('Awaiting measured rate')}{' '}
                  · {t('Daily archive forecast')} ·{' '}
                  {formatBytes(desk.storage?.projected_bytes_per_day)}
                </p>
              </>
            ) : (
              <p className="muted">
                {t(
                  p?.state === 'collecting'
                    ? 'Waiting for the first committed measurement. Check the serial port and receive path if collection stops.'
                    : 'Starting collection opens the selected ports and writes the first measurements to this archive.',
                )}
              </p>
            )}
            {p?.state === 'collecting' && (
              <button
                type="button"
                disabled={desk.busy}
                onClick={() => void desk.command('preparation/stop')}
              >
                {t('Stop collection')}
              </button>
            )}
            {locked &&
              p?.state !== 'collecting' &&
              p?.state !== 'armed' &&
              !p?.duty?.enabled &&
              !p?.baseline_id && (
                <>
                  {features.monitoring_draft_port_correction === true && p && (
                    <DraftPorts
                      key={p.id}
                      project={p}
                      checks={checks}
                      desk={desk}
                      onSaved={(fixed) => {
                        createdProject.current = fixed;
                        setPrepared(fixed);
                      }}
                    />
                  )}
                  <button
                    type="button"
                    disabled={desk.busy || starting}
                    onClick={() => {
                      if (usingSaved && p) {
                        const source = p.primary_source_kind ?? 'tinysa';
                        setKind(source);
                        setLow(firstRanges[source][0]);
                        setHigh(firstRanges[source][1]);
                        setSpacing('10');
                        setAnalyzerPort(p.profile?.analyzer?.port ?? '');
                        setReceiverPort(p.profile?.receiver?.port ?? '');
                        setChecker(!!p.profile?.receiver && source !== 'ardv1');
                      }
                      setUsingSaved(false);
                      setSessionId('');
                      setPrepared(undefined);
                      setStorage('');
                      setConfirm(false);
                      createdProject.current = undefined;
                      createdSession.current = undefined;
                      planSaved.current = false;
                      setStep(0);
                    }}
                  >
                    {t('Change instruments or range in a new draft')}
                  </button>
                  <p className="muted">
                    {t('The earlier project and its archived measurements are preserved.')}
                  </p>
                </>
              )}
            {sampleRead?.error && <p role="alert">{t(sampleRead.error)}</p>}
          </>
        )}
        {step === 3 && (
          <>
            <p>
              {t(
                'Collect at least 20 independent visits over 60 seconds in the ordinary environment before confirming the background.',
              )}
            </p>
            <div className="baseline-status">
              <strong>
                {t(acct?.baseline_ready ? 'Quiet reference ready' : 'Baseline not confirmed')}
              </strong>
              <span>
                <BaselineCounts progress={acct?.baseline_collection} />
              </span>
            </div>
            {!acct?.baseline_ready && !progress?.ready && (
              <p>
                {t('Estimated time to baseline')} ·{' '}
                {estimatedBackground == null
                  ? t('Awaiting measured rate')
                  : formatQuantity(estimatedBackground) + ' ' + t('s')}
              </p>
            )}
            <ul className="check-list">
              {acct?.checks
                .filter((c) => !c.ok)
                .map((c) => (
                  <li key={c.name}>{c.detail}</li>
                ))}
            </ul>
            {features.monitoring_storage_forecast === true && (
              <>
                <ArchiveForecast report={desk.storage} />
                <label>
                  {t('Minimum archive horizon (hours)')}
                  <input
                    inputMode="decimal"
                    required
                    value={hours}
                    onChange={(e) => setHours(e.target.value)}
                  />
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={backup}
                    onChange={(e) => setBackup(e.target.checked)}
                  />
                  {t('Reserve space for a backup')}
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={restore}
                    onChange={(e) => setRestore(e.target.checked)}
                  />
                  {t('Reserve space for a restore check')}
                </label>
                {!validBudget && (
                  <p role="alert">{t('Choose a horizon from one minute to one year.')}</p>
                )}
              </>
            )}
            {!acct?.baseline_ready && (
              <label className="check">
                <input
                  type="checkbox"
                  checked={confirm}
                  onChange={(e) => setConfirm(e.target.checked)}
                />
                {t('I confirm this baseline represents the ordinary environment')}
              </label>
            )}
            {p?.state !== 'collecting' && !acct?.baseline_ready && (
              <button
                type="button"
                disabled={desk.busy || !preparationReady(acct)}
                onClick={() => void desk.command('preparation/start')}
              >
                {t('Start receive collection')}
              </button>
            )}
            {p?.state === 'collecting' && (
              <button
                type="button"
                disabled={desk.busy}
                onClick={() => void desk.command('preparation/stop')}
              >
                {t('Stop collection')}
              </button>
            )}
          </>
        )}
        {desk.action.state === 'Error' && (
          <p className="error-text" role="alert">
            {t(desk.action.text)}
          </p>
        )}
        <div className="actions">
          {step > 0 && step < 3 && !locked && (
            <button type="button" onClick={() => setStep(step - 1)}>
              {t('Back')}
            </button>
          )}
          {step < 4 && (
            <button
              className="primary"
              disabled={
                desk.busy ||
                starting ||
                (step === 1 && !existing && !!invalid) ||
                (step === 2 &&
                  !first &&
                  (!storage ||
                    !checks.receive ||
                    !checks.location ||
                    !checks.notes.trim() ||
                    p?.state === 'collecting')) ||
                (step === 3 &&
                  !acct?.baseline_ready &&
                  (!confirm ||
                    !acct?.baseline_collection?.ready ||
                    (features.monitoring_storage_forecast === true && !validBudget)))
              }
            >
              {t(
                step === 2 && !first
                  ? 'Start receive collection'
                  : step === 3 && !acct?.baseline_ready
                    ? 'Confirm baseline'
                    : 'Next',
              )}
            </button>
          )}
          <button type="button" disabled={desk.busy} onClick={close}>
            {t(locked ? 'Close setup' : 'Cancel')}
          </button>
        </div>
      </form>
      {step === 4 && p && <DutyPolicy desk={desk} onArmed={close} />}
    </Modal>
  );
}
