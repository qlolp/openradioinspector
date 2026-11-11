import { t } from './i18n';
import { encodePathSegment } from './wire';
import { useEffect, useRef, useState } from 'react';
import { historyQuery, type HistoryFilters } from './api';
import { Spectrum } from './charts';
import { Panel, hz, titleCase, utc } from './components';
import { prepareSweep, type UnsupportedSweep } from './pipeline';
import type { Desk } from './useDesk';
import type { Comparison, DisplaySweep, Page, Sweep } from './types';

export function Research({
  desk,
  openOriginal,
}: {
  desk: Desk;
  openOriginal: (id: string) => void;
}) {
  const [filters, setFilters] = useState<HistoryFilters>({}),
    [items, setItems] = useState<Sweep[]>([]),
    [cursor, setCursor] = useState<string | null>(null);
  const [past, setPast] = useState<string[]>([]),
    [after, setAfter] = useState(''),
    [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<DisplaySweep>(),
    [unsupported, setUnsupported] = useState<UnsupportedSweep>(),
    [comparison, setComparison] = useState<Comparison>();
  const [role, setRole] = useState<'receiver' | 'analyzer'>('receiver');
  const [frequency, setFrequency] = useState('100000000'),
    [mode, setMode] = useState('FM'),
    [span, setSpan] = useState('1000000'),
    [eventId, setEventId] = useState(''),
    [mark, setMark] = useState('');
  const leases = useRef(
    new Map<string, Map<string, { token: string; expires: number; returning?: boolean }>>(),
  );
  const [leaseRevision, setLeaseRevision] = useState(0),
    [now, setNow] = useState(Date.now());
  const ticket = useRef(0),
    controller = useRef<AbortController | undefined>(undefined);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    leases.current.clear();
    setLeaseRevision((x) => x + 1);
  }, [desk.connectionVersion]);
  useEffect(() => {
    ticket.current++;
    controller.current?.abort();
    setItems([]);
    setCursor(null);
    setPast([]);
    setAfter('');
    setSelected(undefined);
    setUnsupported(undefined);
    setComparison(undefined);
    setLoaded(false);
    setFilters({});
    setEventId('');
  }, [desk.projectId]);
  useEffect(() => () => controller.current?.abort(), []);
  const lease = leases.current.get(desk.projectId)?.get(role);
  const expired = lease && now >= lease.expires;
  const remote = desk.guard?.manual.find((x) => x.role === role);
  const activeLease =
    !!lease && !expired && !lease.returning && !!remote && !remote.expired && !remote.returning;
  void leaseRevision;

  function selectSource(source?: Sweep) {
    const prepared = source ? prepareSweep(source) : {};
    setSelected(prepared.sweep);
    setUnsupported(prepared.unsupported);
    setComparison(undefined);
    return prepared.sweep;
  }

  async function history(next = '', historyStack: string[] = []) {
    const id = desk.projectId,
      generation = ++ticket.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    await desk.run(
      'Load history',
      async () => {
        const page = await desk.api.project<Page<Sweep>>(
          id,
          'history' + historyQuery(filters, next),
          undefined,
          abort.signal,
        );
        if (generation !== ticket.current || abort.signal.aborted) return;
        setItems(page.items);
        setCursor(page.next_cursor);
        setPast(historyStack);
        setAfter(next);
        setLoaded(true);
        selectSource(page.items[0]);
      },
      [],
    );
  }
  async function take() {
    const id = desk.projectId,
      selectedRole = role;
    await desk.run(
      'Take ' + selectedRole + ' control',
      async () => {
        const value = await desk.api.project<{ token: string; expires_seconds: number }>(
          id,
          'manual/acquire',
          { role: selectedRole },
        );
        const record = leases.current.get(id) || new Map();
        record.set(selectedRole, {
          token: value.token,
          expires: Date.now() + value.expires_seconds * 1000,
        });
        leases.current.set(id, record);
        setLeaseRevision((x) => x + 1);
      },
      ['status'],
    );
  }
  async function ownership(action: 'renew' | 'return') {
    if (!lease) return;
    const id = desk.projectId,
      selectedRole = role;
    await desk.run(
      action + ' ' + role,
      async () => {
        const value = await desk.api.project<{ expires_seconds?: number; state?: string }>(
          id,
          'manual/' + action,
          { token: lease!.token },
        );
        if (action === 'return') {
          if (value.state === 'restoring') lease!.returning = true;
          else leases.current.get(id)?.delete(selectedRole);
        } else lease!.expires = Date.now() + value.expires_seconds! * 1000;
        setLeaseRevision((x) => x + 1);
      },
      ['status', 'search'],
    );
  }
  function tokenAction(label: string, path: string, body: Record<string, unknown>) {
    if (!activeLease) return;
    return desk.run(
      label,
      () => desk.api.project(desk.projectId, path, { token: lease!.token, ...body }),
      ['status', 'search'],
    );
  }
  return (
    <div className="research-grid">
      <Panel
        title={t('Archive research')}
        eyebrow={t('IMMUTABLE MEASUREMENTS')}
        aside={<span className="badge">{t('32 per page')}</span>}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void history();
          }}
        >
          <div className="form-grid filters">
            <label>
              {t('From (UTC)')}
              <input
                type="datetime-local"
                value={filters.start || ''}
                onChange={(e) => setFilters((p) => ({ ...p, start: e.target.value }))}
              />
            </label>
            <label>
              {t('Until (UTC)')}
              <input
                type="datetime-local"
                value={filters.end || ''}
                onChange={(e) => setFilters((p) => ({ ...p, end: e.target.value }))}
              />
            </label>
            <label>
              {t('Minimum frequency (Hz)')}
              <input
                inputMode="numeric"
                pattern="[0-9]*"
                value={filters.min_hz || ''}
                onChange={(e) => setFilters((p) => ({ ...p, min_hz: e.target.value }))}
              />
            </label>
            <label>
              {t('Maximum frequency (Hz)')}
              <input
                inputMode="numeric"
                pattern="[0-9]*"
                value={filters.max_hz || ''}
                onChange={(e) => setFilters((p) => ({ ...p, max_hz: e.target.value }))}
              />
            </label>
            <label>
              {t('Event source filter')}
              <select
                value={filters.event_id || ''}
                onChange={(e) => setFilters((p) => ({ ...p, event_id: e.target.value }))}
              >
                <option value="">{t('All measurements')}</option>
                {desk.events.map((event) => (
                  <option key={event.id} value={event.id}>
                    {hz(event.center_frequency_hz)} · {titleCase(event.kind)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="actions">
            <button className="primary" disabled={desk.busy || !desk.project}>
              {t('Load history')}
            </button>
            <button
              type="button"
              disabled={desk.busy || !past.length}
              onClick={() => void history(past.at(-1), past.slice(0, -1))}
            >
              {t('Previous history page')}
            </button>
            <button
              type="button"
              disabled={desk.busy || !cursor}
              onClick={() => void history(cursor!, [...past, after])}
            >
              {t('Next history page')}
            </button>
            <span className="muted">
              {t('Page') + ' '}
              {past.length + 1}
            </span>
          </div>
        </form>
        {unsupported && (
          <div className="plot-unavailable" role="status" data-testid="research-unsupported-source">
            <strong>{t('Source frequency outside supported display range')}</strong>
            <p>{t('Original measurements remain available without numeric conversion.')}</p>
            <p className="mono">{hz(unsupported.frequency_hz)}</p>
            <button type="button" onClick={() => openOriginal(unsupported.id)}>
              {t('Original sweep')}
            </button>
          </div>
        )}
        {selected && (
          <>
            <Spectrum sweep={selected} comparison={comparison} />
            <div className="chart-caption">
              <span>
                {comparison
                  ? comparison.compatible
                    ? t('Baseline comparison · compatible')
                    : t(comparison.reason || '')
                  : t('Display-only spectrum · original available')}
              </span>
              <span>{comparison?.compatible && t('Current / median / p95')}</span>
            </div>
          </>
        )}
        <div className="history-list">
          {items.map((item) => (
            <div
              data-testid="history-row"
              className={
                'history-row ' + ((selected?.id ?? unsupported?.id) === item.id ? 'selected' : '')
              }
              key={item.id}
            >
              <button
                className="text-button history-select"
                onClick={() => {
                  selectSource(item);
                }}
              >
                <span className="mono">{utc(item.interval.started_at)}</span>
                <small>
                  {item.original_point_count ?? item.levels.length}
                  {' ' + t('original points ·') + ' '}
                  {item.id}
                </small>
              </button>
              <div className="actions">
                <button
                  disabled={desk.busy}
                  onClick={() =>
                    void desk.run(
                      'Compare baseline',
                      async () => {
                        if (!selectSource(item))
                          throw Error('Source frequency outside supported display range');
                        const result = await desk.api.project<Comparison>(
                          desk.projectId,
                          'history/' + encodePathSegment(item.id) + '/comparison',
                        );
                        setComparison(result);
                      },
                      [],
                    )
                  }
                >
                  {t('Compare baseline')}
                </button>
                <button disabled={desk.busy} onClick={() => openOriginal(item.id)}>
                  {t('Original')}
                </button>
              </div>
            </div>
          ))}
        </div>
        {loaded && !items.length && <p className="empty">{t('No sweeps match these filters')}</p>}
        {!loaded && (
          <p className="empty">
            {t(
              'Choose a UTC or frequency range to inspect the archive. Raw measurements remain unchanged.',
            )}
          </p>
        )}
      </Panel>
      <div className="side-stack">
        <Panel title={t('Manual ownership')} eyebrow={t('EXPLICIT · 30 SECOND LEASE')}>
          <p className="muted">
            {t(
              'Taking the analyzer pauses broad coverage. An expired lease keeps automation paused until an explicit return or disarm.',
            )}
          </p>
          <label>
            {t('Instrument role')}
            <select value={role} onChange={(e) => setRole(e.target.value as typeof role)}>
              <option value="receiver">{t('AR-DV1 receiver')}</option>
              <option value="analyzer">{t('tinySA analyzer')}</option>
            </select>
          </label>
          <p className="lease-state">
            {t('{role} lease ·', { role: titleCase(role) })}{' '}
            {remote?.returning || lease?.returning
              ? t('restoring')
              : expired || remote?.expired
                ? t('expired — explicit return required')
                : activeLease
                  ? t('active')
                  : remote
                    ? t('owned by another client')
                    : t('not held')}
            {activeLease && (
              <span className="mono">
                {' '}
                · {Math.max(0, Math.ceil((lease!.expires - now) / 1000))}
                {' ' + t('s')}
              </span>
            )}
          </p>
          <div className="actions">
            <button
              disabled={desk.busy || !desk.guard?.active || !!remote}
              onClick={() => void take()}
            >
              {t(role === 'receiver' ? 'Take receiver control' : 'Take analyzer control')}
            </button>
            <button disabled={desk.busy || !activeLease} onClick={() => void ownership('renew')}>
              {t('Renew lease')}
            </button>
            <button
              disabled={desk.busy || !lease || !!lease.returning}
              onClick={() => void ownership('return')}
            >
              {t(role === 'receiver' ? 'Return receiver' : 'Return analyzer')}
            </button>
          </div>
          {role === 'receiver' ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void tokenAction('Tune receiver', 'manual/tune', {
                  frequency_hz: Number(frequency),
                  mode,
                });
              }}
            >
              <label>
                {t('Tune frequency (Hz)')}
                <input
                  inputMode="numeric"
                  pattern="[0-9]+"
                  required
                  value={frequency}
                  onChange={(e) => setFrequency(e.target.value)}
                />
              </label>
              <label>
                {t('Receive mode')}
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  {['FM', 'AM', 'AUTO'].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <button className="primary" disabled={desk.busy || !activeLease}>
                {t('Tune receiver')}
              </button>
            </form>
          ) : (
            <>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void tokenAction('Start narrow search', 'search/start', {
                    event_id: eventId,
                    span_hz: Number(span),
                  });
                }}
              >
                <label>
                  {t('Source event')}
                  <select required value={eventId} onChange={(e) => setEventId(e.target.value)}>
                    <option value="">{t('Choose a detected event')}</option>
                    {desk.events.map((e) => (
                      <option key={e.id} value={e.id}>
                        {hz(e.center_frequency_hz)} · {titleCase(e.kind)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  {t('Search span (Hz)')}
                  <input
                    type="number"
                    min={1000}
                    max={100000000}
                    step={1}
                    required
                    value={span}
                    onChange={(e) => setSpan(e.target.value)}
                  />
                </label>
                <button
                  className="primary"
                  disabled={desk.busy || !activeLease || desk.search?.active}
                >
                  {t('Start narrow search')}
                </button>
              </form>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void tokenAction('Mark position', 'search/mark', { label: mark });
                }}
              >
                <label>
                  {t('Position label')}
                  <input
                    required
                    value={mark}
                    maxLength={200}
                    onChange={(e) => setMark(e.target.value)}
                  />
                </label>
                <button disabled={desk.busy || !activeLease || !desk.search?.active}>
                  {t('Mark position')}
                </button>
              </form>
            </>
          )}
        </Panel>
        <Panel
          title={t('Source search')}
          eyebrow={titleCase(desk.search?.coverage || t('stopped'))}
        >
          <p>
            {t('Maximum from core')}{' '}
            <strong className="mono">
              {desk.search?.maximum_level ?? '—'}
              {' ' + t('dBm')}
            </strong>
          </p>
          <p className="muted">
            {desk.search?.series.length || 0}
            {t('/256 recent source sweeps · broad coverage')}{' '}
            {desk.search?.coverage || t('stopped')}
          </p>
          {desk.search?.error && <p className="error-text">{desk.search.error}</p>}
          <ol className="marks">
            {desk.search?.marks.map((item) => (
              <li key={item.id}>
                {item.label}
                <span className="mono">
                  {' '}
                  {item.level}
                  {' ' + t('dBm ·') + ' '}
                  {hz(item.frequency_hz)}
                </span>
              </li>
            ))}
          </ol>
        </Panel>
      </div>
    </div>
  );
}
