import { useState } from 'react';
import { hz, Modal } from './components';
import { getLocale, t } from './i18n';
import {
  boardProfiles,
  latestRfReadback,
  rangeEndpoints,
  rfExplorerSession,
  surveyCounts,
  validateRfRange,
  type RfBounds,
  type RfExplorerReadback,
  type RfRangeEntry,
} from './rf-explorer';
import { message, remember, type Desk } from './useDesk';
import type { Session } from './types';

const presets = [
  { name: '80–120 MHz', start_hz: 80_000_000, stop_hz: 120_000_000 },
  { name: '100–400 MHz', start_hz: 100_000_000, stop_hz: 400_000_000 },
];
type Preset = RfBounds & { name: string };
const storageKey = 'rf-explorer-range-presets';

function savedPresets(): Preset[] {
  try {
    const values: unknown = JSON.parse(remember(storageKey, '[]'));
    if (!Array.isArray(values)) return [];
    return values.filter((p) => {
      if (typeof p?.name !== 'string' || !p.name.trim() || p.name.length > 80) return false;
      try {
        validateRfRange({ start: String(p.start_hz), stop: String(p.stop_hz) }, true);
        return true;
      } catch {
        return false;
      }
    });
  } catch {
    return [];
  }
}
const count = (n: number) => n.toLocaleString(getLocale() === 'ru' ? 'ru-RU' : 'en-US');

function counts(bounds: RfBounds) {
  const { points, windows } = surveyCounts(bounds);
  return t('{windows} windows · {points} unique frequencies', {
    windows: count(windows),
    points: count(points),
  });
}
export function ReportedBoard({ readback }: { readback?: RfExplorerReadback }) {
  if (!readback?.active_board || !readback.active_input) return <>{t('Awaiting board readback')}</>;
  return (
    <>
      {t(readback.active_input === 'left' ? 'Left SMA' : 'Right SMA')} ·{' '}
      {readback.active_board === 'main' ? 'WSUB1G' : 'WSUB3G'}
    </>
  );
}

export function RfPlanPreview({
  bounds,
  survey,
  readback,
}: {
  bounds?: RfBounds;
  survey: boolean;
  readback?: RfExplorerReadback;
}) {
  const reported =
    readback?.board_min_hz != null && readback.board_max_hz != null
      ? { start_hz: readback.board_min_hz, stop_hz: readback.board_max_hz }
      : undefined;
  const plan = bounds ?? reported;
  const knownStep =
    !survey && bounds && readback?.points && readback.points > 1
      ? (bounds.stop_hz - bounds.start_hz) / (readback.points - 1)
      : undefined;
  const outside =
    bounds &&
    reported &&
    (bounds.start_hz < reported.start_hz || bounds.stop_hz > reported.stop_hz);
  const expected = Object.values(boardProfiles).filter(
    (p) => !bounds || (p.low <= bounds.start_hz && p.high >= bounds.stop_hz),
  );
  return (
    <div className="rf-plan-preview">
      <dl className="status-list">
        <div>
          <dt>{t(readback?.simulated ? 'Last simulated input' : 'Last reported input')}</dt>
          <dd>
            <ReportedBoard readback={readback} />
          </dd>
        </div>
        {reported && (
          <div>
            <dt>{t(readback?.simulated ? 'Simulated board limits' : 'Reported board limits')}</dt>
            <dd>
              {hz(reported.start_hz)} – {hz(reported.stop_hz)}
            </dd>
          </div>
        )}
        <div>
          <dt>{t('Expected input limits')}</dt>
          <dd>
            {expected.map((p) => (
              <div key={p.label}>
                {t(p.label)} · {hz(p.low)} – {hz(p.high)}
                {survey && !plan && (
                  <small> · {counts({ start_hz: p.low, stop_hz: p.high })}</small>
                )}
              </div>
            ))}
          </dd>
        </div>
        {survey && (
          <div>
            <dt>{t('Survey grid')}</dt>
            <dd>
              {hz(10000)}
              {plan && <> · {counts(plan)}</>}
            </dd>
          </div>
        )}
        {!survey && bounds && (
          <div>
            <dt>{t('Bin spacing')}</dt>
            <dd>
              {knownStep
                ? Number.isInteger(knownStep)
                  ? hz(knownStep)
                  : t('No whole-Hz step for the reported point count')
                : t('Awaiting board readback')}
            </dd>
          </div>
        )}
        {readback?.rbw_hz && (
          <div>
            <dt>{t('Reported RBW')}</dt>
            <dd>{hz(readback.rbw_hz)}</dd>
          </div>
        )}
        {readback?.offset_db != null && (
          <div>
            <dt>{t('Reported amplitude offset')}</dt>
            <dd>{readback.offset_db} dB</dd>
          </div>
        )}
        {plan && (
          <div>
            <dt>{t('Planned range')}</dt>
            <dd>
              {hz(plan.start_hz)} – {hz(plan.stop_hz)}
            </dd>
          </div>
        )}
      </dl>
      {outside && (
        <p className="warning-text">
          {t(
            'This range exceeds the last reported input limits. Confirm the active SMA on the analyzer before starting.',
          )}
        </p>
      )}
    </div>
  );
}

export function RfExplorerNewSession({
  desk,
  select,
  close,
}: {
  desk: Desk;
  select: (id: string) => void;
  close: () => void;
}) {
  const [name, setName] = useState(''),
    [port, setPort] = useState('');
  const [mode, setMode] = useState('follow'),
    [entry, setEntry] = useState<'edges' | 'center'>('edges');
  const [first, setFirst] = useState(''),
    [second, setSecond] = useState('');
  const [error, setError] = useState(''),
    [preset, setPreset] = useState('custom');
  const [saved, setSaved] = useState(savedPresets),
    [presetName, setPresetName] = useState('');
  const supportsSurvey = desk.capabilities?.features.rf_explorer_10khz_survey === true;
  const survey = supportsSurvey && (mode === 'survey-module' || mode === 'survey-range');
  const custom = mode === 'fixed' || (survey && mode === 'survey-range');
  const fields: RfRangeEntry =
    entry === 'edges' ? { start: first, stop: second } : { center: first, span: second };
  let preview: RfBounds | undefined;
  if (custom) {
    try {
      preview = validateRfRange(fields, survey);
    } catch {
      /* Keep partial input editable. */
    }
  }
  const current = desk.rfSession;
  const readback =
    current && current.streams[0].port === port.trim()
      ? latestRfReadback(desk.live.rows, current.id, current.streams[0].id)?.readback
      : undefined;
  function writeFields(bounds: RfBounds, target: 'edges' | 'center') {
    const { start_hz, stop_hz } = bounds;
    setFirst(String(target === 'edges' ? start_hz : (start_hz + stop_hz) / 2));
    setSecond(String(target === 'edges' ? stop_hz : stop_hz - start_hz));
  }
  function switchEntry(target: 'edges' | 'center') {
    if (first || second) {
      try {
        writeFields(rangeEndpoints(fields), target);
      } catch (cause) {
        setError(message(cause));
        return;
      }
    }
    setEntry(target);
    setError('');
  }
  function chooseMode(next: string) {
    setMode(next);
    setPreset(next === 'survey-module' ? 'board' : 'custom');
    setError('');
  }
  function choosePreset(key: string) {
    setPreset(key);
    setError('');
    setMode(key === 'board' ? 'survey-module' : 'survey-range');
    const chosen = [...presets, ...saved][Number(key)];
    if (chosen) writeFields(chosen, entry);
  }
  function savePreset() {
    try {
      const bounds = validateRfRange(fields, true),
        name = presetName.trim();
      if (!name) return;
      const next = [...saved.filter((p) => p.name !== name), { name, ...bounds }];
      try {
        localStorage.setItem('ori-modern-' + storageKey, JSON.stringify(next));
      } catch {
        throw Error('This client could not save the preset');
      }
      setSaved(next);
      setPreset(String(presets.length + next.length - 1));
      setError('');
    } catch (cause) {
      setError(message(cause));
    }
  }
  function create() {
    try {
      const body = rfExplorerSession(name, port, { survey, ...(custom && fields) });
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
              checked={custom}
              onChange={(e) => chooseMode(e.target.checked ? 'fixed' : 'follow')}
            />
            {t('Set a sweep window')}
          </label>
        )}
        {survey && (
          <label>
            {t('Range preset')}
            <select value={preset} onChange={(e) => choosePreset(e.target.value)}>
              <option value="custom">{t('Custom range')}</option>
              <option value="board">{t('Full active board')}</option>
              {[...presets, ...saved].map((p, i) => (
                <option key={i} value={String(i)}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {custom && (
          <>
            <label>
              {t('Frequency entry')}
              <select value={entry} onChange={(e) => switchEntry(e.target.value as typeof entry)}>
                <option value="edges">{t('Start / Stop')}</option>
                <option value="center">{t('Center / Span')}</option>
              </select>
            </label>
            <div className="form-grid">
              <label>
                {t(entry === 'edges' ? 'Start frequency (Hz)' : 'Center frequency (Hz)')}
                <input
                  type="number"
                  required
                  min={entry === 'edges' ? 1000 : 1}
                  step={entry === 'edges' ? 1000 : 1}
                  value={first}
                  onChange={(e) => {
                    setFirst(e.target.value);
                    setPreset('custom');
                    setError('');
                  }}
                />
              </label>
              <label>
                {t(entry === 'edges' ? 'Stop frequency (Hz)' : 'Span (Hz)')}
                <input
                  type="number"
                  required
                  min={entry === 'edges' ? 1000 : 1}
                  step={entry === 'edges' ? 1000 : 1}
                  value={second}
                  onChange={(e) => {
                    setSecond(e.target.value);
                    setPreset('custom');
                    setError('');
                  }}
                />
              </label>
            </div>
          </>
        )}
        {survey && mode === 'survey-range' && (
          <p className="muted">
            {t(
              'Both endpoints are included. Enter multiples of 1,000 Hz with a span of at least 1.11 MHz in 10 kHz steps.',
            )}
          </p>
        )}
        {mode === 'follow' && (
          <p className="muted">{t('Acquisition follows the active window on the analyzer.')}</p>
        )}
        {(preview || mode === 'survey-module') && (
          <RfPlanPreview bounds={preview} survey={survey} readback={readback} />
        )}
        {survey && custom && (
          <div className="actions rf-inline-form">
            <label>
              {t('Preset name')}
              <input
                value={presetName}
                maxLength={80}
                onChange={(e) => setPresetName(e.target.value)}
              />
            </label>
            <button type="button" disabled={!preview || !presetName.trim()} onClick={savePreset}>
              {t('Save preset locally')}
            </button>
          </div>
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
