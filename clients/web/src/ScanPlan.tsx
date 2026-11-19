import { useEffect, useState, type FormEvent } from 'react';
import { Panel } from './components';
import { t } from './i18n';
import { gridPointCount, coverageBands } from './scan-plan';
import { CoverageRow } from './StationPanels';
import type { Desk } from './useDesk';
import type { ScanBand } from '../../../contracts/generated/typescript/runtime';

type Draft = {
  id: string;
  name: string;
  start_hz: string;
  stop_hz: string;
  points: string;
  step_hz: string;
  rbw_hz: string;
  max_revisit_seconds: string;
};
const initial = (): Draft => ({
  id: crypto.randomUUID(),
  name: t('Band'),
  start_hz: '88000000',
  stop_hz: '108000000',
  points: '201',
  step_hz: '',
  rbw_hz: '10000',
  max_revisit_seconds: '15',
});
const fromBand = (band: ScanBand): Draft => ({
  id: band.id,
  name: band.name,
  start_hz: String(band.start_hz),
  stop_hz: String(band.stop_hz),
  points: String(band.points),
  step_hz: String(band.step_hz ?? ''),
  rbw_hz: String(band.rbw_hz),
  max_revisit_seconds: String(band.max_revisit_seconds),
});

export function ScanPlan({ desk }: { desk: Desk }) {
  const [name, setName] = useState(() => t('Site survey')),
    [bands, setBands] = useState<Draft[]>(() => [initial()]);
  const [confirmed, setConfirmed] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState('');
  const project = desk.project;
  const gridSupported = desk.capabilities?.features.scan_plan_grid_10khz === true;
  useEffect(() => {
    const saved = project?.scan_plan;
    setName(saved?.config.name ?? t('Site survey'));
    const first = initial();
    const settings = project?.profile?.analyzer.settings;
    if (settings)
      for (const key of ['start_hz', 'stop_hz', 'points', 'rbw_hz'] as const)
        if (settings[key] !== undefined) first[key] = String(settings[key]);
    setBands(saved ? saved.config.bands.map(fromBand) : [first]);
    setConfirmed(new Set());
    setError('');
  }, [project?.id, project?.scan_plan?.id]);
  const editable = !!project && !desk.busy && !desk.guard?.active && project.state !== 'collecting';
  const update = (id: string, key: keyof Draft, value: string) =>
    setBands((prior) => prior.map((b) => (b.id === id ? { ...b, [key]: value } : b)));
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    const config = bands.map((band) => ({
      id: band.id,
      name: band.name,
      start_hz: Number(band.start_hz),
      stop_hz: Number(band.stop_hz),
      points: band.step_hz ? gridPointCount(band.start_hz, band.stop_hz) : Number(band.points),
      ...(band.step_hz ? { step_hz: 10000 } : {}),
      rbw_hz: Number(band.rbw_hz),
      max_revisit_seconds: Number(band.max_revisit_seconds),
    }));
    if (
      config.some(
        (b) =>
          !Number.isSafeInteger(b.points) ||
          !Number.isFinite(b.max_revisit_seconds) ||
          ![b.start_hz, b.stop_hz, b.rbw_hz].every(Number.isSafeInteger) ||
          (b.step_hz && !gridSupported),
      )
    ) {
      setError(
        'Choose integer frequencies and a whole number of 10 kHz steps within the supported band.',
      );
      return;
    }
    await desk.command('scan-plan', { name, bands: config }, ['status', 'preflight']);
  }
  if (desk.capabilities?.features.monitoring_scan_plans !== true) return null;
  const worker = desk.guard?.workers.find(
    (entry) => entry.stream_id === project?.analyzer_stream_id,
  );
  return (
    <Panel title={t('Scan plan')}>
      <p className="muted">
        {t('The analyzer visits bands in order. A revised plan needs new baseline confirmation.')}
      </p>
      <form onSubmit={(event) => void submit(event)}>
        <fieldset disabled={!editable} className="plan-form">
          <label>
            {t('Plan name')}
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          {bands.map((band, index) => (
            <fieldset key={band.id}>
              <legend>
                {t('Band')} {index + 1}
              </legend>
              {gridSupported && (
                <button
                  type="button"
                  onClick={() =>
                    setBands((previous) =>
                      previous.map((item) =>
                        item.id === band.id
                          ? { ...item, start_hz: '100000', stop_hz: '1300000000', step_hz: '10000' }
                          : item,
                      ),
                    )
                  }
                >
                  {t('Use 100 kHz–1.3 GHz grid')}
                </button>
              )}
              <div className="form-grid">
                <label>
                  {t('Band name')}
                  <input
                    required
                    maxLength={200}
                    value={band.name}
                    onChange={(e) => update(band.id, 'name', e.target.value)}
                  />
                </label>
                {gridSupported && (
                  <label>
                    {t('Frequency grid')}
                    <select
                      value={band.step_hz}
                      onChange={(e) => update(band.id, 'step_hz', e.target.value)}
                    >
                      <option value="">{t('Fixed point count')}</option>
                      <option value="10000">{t('10 kHz steps')}</option>
                    </select>
                  </label>
                )}
                <label>
                  {t('Band start (Hz)')}
                  <input
                    type="number"
                    step="1"
                    min="100000"
                    max={band.step_hz ? 1300000000 : 800000000}
                    required
                    value={band.start_hz}
                    onChange={(e) => update(band.id, 'start_hz', e.target.value)}
                  />
                </label>
                <label>
                  {t('Band end (Hz)')}
                  <input
                    type="number"
                    step="1"
                    min="100000"
                    max={band.step_hz ? 1300000000 : 800000000}
                    required
                    value={band.stop_hz}
                    onChange={(e) => update(band.id, 'stop_hz', e.target.value)}
                  />
                </label>
                <label>
                  {t('Logical points')}
                  <input
                    type="number"
                    required={!band.step_hz}
                    readOnly={!!band.step_hz}
                    min="2"
                    max={band.step_hz ? 129991 : 290}
                    value={
                      band.step_hz
                        ? (gridPointCount(band.start_hz, band.stop_hz) ?? '')
                        : band.points
                    }
                    onChange={(e) => update(band.id, 'points', e.target.value)}
                  />
                </label>
                <label>
                  {t('Maximum revisit (s)')}
                  <input
                    type="number"
                    required
                    min="1"
                    max="3600"
                    step="any"
                    value={band.max_revisit_seconds}
                    onChange={(e) => update(band.id, 'max_revisit_seconds', e.target.value)}
                  />
                </label>
                <label>
                  {t('Requested RBW')}
                  <select
                    value={band.rbw_hz}
                    onChange={(e) => update(band.id, 'rbw_hz', e.target.value)}
                  >
                    <option value="10000">{t('10 kHz · detailed')}</option>
                    <option value="30000">{t('30 kHz · fast')}</option>
                  </select>
                </label>
              </div>
              {band.step_hz && (
                <p className="muted">
                  {t(
                    'Each hardware window is archived separately. Frequencies above 800 MHz require the exact Ultra firmware to pass the node’s identity check.',
                  )}
                </p>
              )}
              <button
                type="button"
                disabled={bands.length <= 1}
                onClick={() => setBands((prior) => prior.filter((b) => b.id !== band.id))}
              >
                {t('Remove band')}
              </button>
            </fieldset>
          ))}
          <div className="actions">
            <button
              type="button"
              disabled={bands.length >= 16}
              onClick={() =>
                setBands((prior) => [
                  ...prior,
                  { ...initial(), name: t('Band') + ' ' + (prior.length + 1) },
                ])
              }
            >
              {t('Add band')}
            </button>
            <button>{t('Save plan revision')}</button>
          </div>
        </fieldset>
      </form>
      {error && <p role="alert">{t(error)}</p>}
      {desk.preflight?.baseline_collection.bands?.map((band) => (
        <div key={band.band_id} className="band-baseline">
          <strong>
            {band.name} · {t(band.baseline_id ? 'Quiet reference ready' : 'Baseline not confirmed')}
          </strong>
          <p>
            {band.sweeps} / {band.minimum_sweeps} {t('sweeps')} · {band.elapsed_seconds.toFixed(1)}{' '}
            / {band.minimum_seconds} {t('s')}
          </p>
          {band.window_count != null && (
            <p>
              {t('Windows ready')} {band.ready_windows ?? '—'} / {band.window_count} ·{' '}
              {t('Confirmed windows')} {band.confirmed_windows ?? '—'}
            </p>
          )}
          <label className="check">
            <input
              type="checkbox"
              checked={confirmed.has(band.band_id)}
              onChange={(event) =>
                setConfirmed((prior) => {
                  const next = new Set(prior);
                  if (event.target.checked) next.add(band.band_id);
                  else next.delete(band.band_id);
                  return next;
                })
              }
            />
            {t('Ordinary environment confirmed for this band')}
          </label>
          <button
            disabled={
              desk.busy || !!desk.guard?.active || !band.ready || !confirmed.has(band.band_id)
            }
            onClick={() =>
              void desk.command(
                'baselines',
                {
                  name: [...(name + ' · ' + band.name)].slice(0, 200).join(''),
                  location: project?.site ?? '',
                  confirm: true,
                  band_id: band.band_id,
                },
                ['status', 'preflight'],
              )
            }
          >
            {t('Confirm band baseline')}
          </button>
        </div>
      ))}
      {coverageBands(worker).map((band) => (
        <CoverageRow key={band.id} band={band} />
      ))}
    </Panel>
  );
}
