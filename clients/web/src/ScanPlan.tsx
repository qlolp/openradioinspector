import { useEffect, useState, type FormEvent } from 'react';
import { Panel } from './components';
import { t } from './i18n';
import type { Desk } from './useDesk';
import type { ScanBand } from '../../../contracts/generated/typescript/runtime';

type Draft = {
  id: string;
  name: string;
  start_hz: string;
  stop_hz: string;
  points: string;
  rbw_hz: string;
  max_revisit_seconds: string;
};
const initial = (): Draft => ({
  id: crypto.randomUUID(),
  name: 'Поддиапазон',
  start_hz: '88000000',
  stop_hz: '108000000',
  points: '201',
  rbw_hz: '10000',
  max_revisit_seconds: '15',
});
const fromBand = (band: ScanBand): Draft =>
  Object.fromEntries(Object.entries(band).map(([key, value]) => [key, String(value)])) as Draft;

export function ScanPlan({ desk }: { desk: Desk }) {
  const [name, setName] = useState('Обзор объекта'),
    [bands, setBands] = useState<Draft[]>(() => [initial()]);
  const [confirmed, setConfirmed] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState('');
  const project = desk.project;
  useEffect(() => {
    const saved = project?.scan_plan;
    setName(saved?.config.name ?? 'Обзор объекта');
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
  async function save(event: FormEvent) {
    event.preventDefault();
    setError('');
    const config = bands.map((band) => ({
      id: band.id,
      name: band.name,
      start_hz: Number(band.start_hz),
      stop_hz: Number(band.stop_hz),
      points: Number(band.points),
      rbw_hz: Number(band.rbw_hz),
      max_revisit_seconds: Number(band.max_revisit_seconds),
    }));
    if (
      config.some(
        (b) =>
          ![b.start_hz, b.stop_hz, b.points, b.rbw_hz].every(Number.isSafeInteger) ||
          !Number.isFinite(b.max_revisit_seconds),
      )
    ) {
      setError('Укажите целые частоты в Гц, число точек и конечный срок возврата.');
      return;
    }
    await desk.command('scan-plan', { name, bands: config }, ['status', 'preflight']);
  }
  if (desk.capabilities?.features.monitoring_scan_plans !== true) return null;
  return (
    <Panel title={t('План обзора')} eyebrow="ПОСЛЕДОВАТЕЛЬНЫЕ ПОДДИАПАЗОНЫ">
      <p className="muted">
        Один анализатор обходит диапазоны последовательно. Сохранение новой версии требует
        подтверждения новых эталонов; приборы этой кнопкой не запускаются.
      </p>
      <form onSubmit={(event) => void save(event)}>
        <fieldset disabled={!editable}>
          <label>
            Название плана
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          {bands.map((band, index) => (
            <fieldset key={band.id}>
              <legend>Поддиапазон {index + 1}</legend>
              <div className="form-grid">
                <label>
                  Название
                  <input
                    required
                    maxLength={200}
                    value={band.name}
                    onChange={(e) => update(band.id, 'name', e.target.value)}
                  />
                </label>
                {(['start_hz', 'stop_hz', 'points', 'max_revisit_seconds'] as const).map((key) => (
                  <label key={key}>
                    {
                      {
                        start_hz: 'Начало, Гц',
                        stop_hz: 'Конец, Гц',
                        points: 'Точек',
                        max_revisit_seconds: 'Предельный возврат, с',
                      }[key]
                    }
                    <input
                      required
                      inputMode="decimal"
                      value={band[key]}
                      onChange={(e) => update(band.id, key, e.target.value)}
                    />
                  </label>
                ))}
                <label>
                  RBW, Гц
                  <select
                    value={band.rbw_hz}
                    onChange={(e) => update(band.id, 'rbw_hz', e.target.value)}
                  >
                    <option value="10000">10000</option>
                    <option value="30000">30000</option>
                  </select>
                </label>
              </div>
              <button
                type="button"
                disabled={bands.length <= 1}
                onClick={() => setBands((prior) => prior.filter((b) => b.id !== band.id))}
              >
                Убрать из черновика
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
                  { ...initial(), name: `Поддиапазон ${prior.length + 1}` },
                ])
              }
            >
              Добавить поддиапазон
            </button>
            <button>Сохранить новую версию</button>
          </div>
        </fieldset>
      </form>
      {error && <p role="alert">{error}</p>}
      <p>
        Текущий приёмный профиль: 0,1–800 МГц, 2–290 точек, RBW 10/30 кГц. Обзор 2,4/5 ГГц требует
        отдельно проверенного профиля.
      </p>
      {desk.preflight?.baseline_collection.bands?.map((band) => (
        <div key={band.band_id} className="baseline-status">
          <strong>
            {band.name} · {band.baseline_id ? 'Эталон подтверждён' : 'Нужен эталон'}
          </strong>
          <span>
            {band.sweeps} / {band.minimum_sweeps} свипов · {band.elapsed_seconds.toFixed(1)} /{' '}
            {band.minimum_seconds} с
          </span>
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
            Обычная обстановка для этого поддиапазона подтверждена
          </label>
          <button
            disabled={
              desk.busy || !!desk.guard?.active || !band.ready || !confirmed.has(band.band_id)
            }
            onClick={() =>
              void desk.command(
                'baselines',
                {
                  name: [...`${name} · ${band.name}`].slice(0, 200).join(''),
                  location: project?.site ?? '',
                  confirm: true,
                  band_id: band.band_id,
                },
                ['status', 'preflight'],
              )
            }
          >
            Подтвердить эталон поддиапазона
          </button>
        </div>
      ))}
      {desk.guard?.workers
        .flatMap((worker) => {
          const coverage = worker.current.scan_coverage as
            | {
                bands?: {
                  id: string;
                  name: string;
                  state: string;
                  deadline_missed: boolean;
                  age_seconds: number | null;
                  last_revisit_seconds: number | null;
                }[];
              }
            | undefined;
          return coverage?.bands ?? [];
        })
        .map((band) => (
          <p key={band.id}>
            {band.name}:{' '}
            {(
              {
                fresh: 'Обзор актуален',
                stale: 'Измерение устарело',
                paused: 'Обзор приостановлен',
                not_observed: 'Измерений ещё нет',
              } as Record<string, string>
            )[band.state] ?? band.state}{' '}
            · возраст {band.age_seconds?.toFixed(1) ?? '—'} с · возврат{' '}
            {band.last_revisit_seconds?.toFixed(1) ?? '—'} с
            {band.deadline_missed && ' · Превышен срок возврата'}
          </p>
        ))}
    </Panel>
  );
}
