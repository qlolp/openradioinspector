import { useEffect, useRef, useState } from 'react';
import { historyQuery, type HistoryFilters } from './api';
import { Spectrum } from './charts';
import { Panel, hz, titleCase, utc } from './components';
import { decimate } from './pipeline';
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
        setSelected(page.items[0] ? decimate(page.items[0]) : undefined);
        setComparison(undefined);
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
        title="Archive research"
        eyebrow="IMMUTABLE MEASUREMENTS"
        aside={<span className="badge">32 per page</span>}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void history();
          }}
        >
          <div className="form-grid filters">
            <label>
              From (UTC)
              <input
                type="datetime-local"
                value={filters.start || ''}
                onChange={(e) => setFilters((p) => ({ ...p, start: e.target.value }))}
              />
            </label>
            <label>
              Until (UTC)
              <input
                type="datetime-local"
                value={filters.end || ''}
                onChange={(e) => setFilters((p) => ({ ...p, end: e.target.value }))}
              />
            </label>
            <label>
              Minimum frequency (Hz)
              <input
                inputMode="numeric"
                pattern="[0-9]*"
                value={filters.min_hz || ''}
                onChange={(e) => setFilters((p) => ({ ...p, min_hz: e.target.value }))}
              />
            </label>
            <label>
              Maximum frequency (Hz)
              <input
                inputMode="numeric"
                pattern="[0-9]*"
                value={filters.max_hz || ''}
                onChange={(e) => setFilters((p) => ({ ...p, max_hz: e.target.value }))}
              />
            </label>
            <label>
              Event source filter
              <select
                value={filters.event_id || ''}
                onChange={(e) => setFilters((p) => ({ ...p, event_id: e.target.value }))}
              >
                <option value="">All measurements</option>
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
              Load history
            </button>
            <button
              type="button"
              disabled={desk.busy || !past.length}
              onClick={() => void history(past.at(-1), past.slice(0, -1))}
            >
              Previous history page
            </button>
            <button
              type="button"
              disabled={desk.busy || !cursor}
              onClick={() => void history(cursor!, [...past, after])}
            >
              Next history page
            </button>
            <span className="muted">Page {past.length + 1}</span>
          </div>
        </form>
        {selected && (
          <>
            <Spectrum sweep={selected} comparison={comparison} />
            <div className="chart-caption">
              <span>
                {comparison
                  ? comparison.compatible
                    ? 'Baseline comparison · compatible'
                    : comparison.reason
                  : 'Display-only spectrum · original available'}
              </span>
              <span>{comparison?.compatible && 'Current / median / p95'}</span>
            </div>
          </>
        )}
        <div className="history-list">
          {items.map((item) => (
            <div
              data-testid="history-row"
              className={'history-row ' + (selected?.id === item.id ? 'selected' : '')}
              key={item.id}
            >
              <button
                className="text-button history-select"
                onClick={() => {
                  setSelected(decimate(item));
                  setComparison(undefined);
                }}
              >
                <span className="mono">{utc(item.interval.started_at)}</span>
                <small>
                  {item.original_point_count ?? item.levels.length} original points · {item.id}
                </small>
              </button>
              <div className="actions">
                <button
                  disabled={desk.busy}
                  onClick={() =>
                    void desk.run(
                      'Compare baseline',
                      async () => {
                        const result = await desk.api.project<Comparison>(
                          desk.projectId,
                          'history/' + encodeURIComponent(item.id) + '/comparison',
                        );
                        setSelected(decimate(item));
                        setComparison(result);
                      },
                      [],
                    )
                  }
                >
                  Compare baseline
                </button>
                <button disabled={desk.busy} onClick={() => openOriginal(item.id)}>
                  Original
                </button>
              </div>
            </div>
          ))}
        </div>
        {loaded && !items.length && <p className="empty">No sweeps match these filters</p>}
        {!loaded && (
          <p className="empty">
            Choose a UTC or frequency range to inspect the archive. Raw measurements remain
            unchanged.
          </p>
        )}
      </Panel>
      <div className="side-stack">
        <Panel title="Manual ownership" eyebrow="EXPLICIT · 30 SECOND LEASE">
          <p className="muted">
            Taking the analyzer pauses broad coverage. An expired lease keeps automation paused
            until an explicit return or disarm.
          </p>
          <label>
            Instrument role
            <select value={role} onChange={(e) => setRole(e.target.value as typeof role)}>
              <option value="receiver">AR-DV1 receiver</option>
              <option value="analyzer">tinySA analyzer</option>
            </select>
          </label>
          <p className="lease-state">
            {titleCase(role)} lease ·{' '}
            {remote?.returning || lease?.returning
              ? 'restoring'
              : expired || remote?.expired
                ? 'expired — explicit return required'
                : activeLease
                  ? 'active'
                  : remote
                    ? 'owned by another client'
                    : 'not held'}
            {activeLease && (
              <span className="mono">
                {' '}
                · {Math.max(0, Math.ceil((lease!.expires - now) / 1000))} s
              </span>
            )}
          </p>
          <div className="actions">
            <button
              disabled={desk.busy || !desk.guard?.active || !!remote}
              onClick={() => void take()}
            >
              Take {role} control
            </button>
            <button disabled={desk.busy || !activeLease} onClick={() => void ownership('renew')}>
              Renew lease
            </button>
            <button
              disabled={desk.busy || !lease || !!lease.returning}
              onClick={() => void ownership('return')}
            >
              Return {role}
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
                Tune frequency (Hz)
                <input
                  inputMode="numeric"
                  pattern="[0-9]+"
                  required
                  value={frequency}
                  onChange={(e) => setFrequency(e.target.value)}
                />
              </label>
              <label>
                Receive mode
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  {['FM', 'AM', 'AUTO'].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <button className="primary" disabled={desk.busy || !activeLease}>
                Tune receiver
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
                  Source event
                  <select required value={eventId} onChange={(e) => setEventId(e.target.value)}>
                    <option value="">Choose a detected event</option>
                    {desk.events.map((e) => (
                      <option key={e.id} value={e.id}>
                        {hz(e.center_frequency_hz)} · {titleCase(e.kind)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Search span (Hz)
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
                  Start narrow search
                </button>
              </form>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void tokenAction('Mark position', 'search/mark', { label: mark });
                }}
              >
                <label>
                  Position label
                  <input
                    required
                    value={mark}
                    maxLength={200}
                    onChange={(e) => setMark(e.target.value)}
                  />
                </label>
                <button disabled={desk.busy || !activeLease || !desk.search?.active}>
                  Mark position
                </button>
              </form>
            </>
          )}
        </Panel>
        <Panel title="Source search" eyebrow={titleCase(desk.search?.coverage || 'stopped')}>
          <p>
            Maximum from core{' '}
            <strong className="mono">{desk.search?.maximum_level ?? '—'} dBm</strong>
          </p>
          <p className="muted">
            {desk.search?.series.length || 0}/256 recent source sweeps · broad coverage{' '}
            {desk.search?.coverage || 'stopped'}
          </p>
          {desk.search?.error && <p className="error-text">{desk.search.error}</p>}
          <ol className="marks">
            {desk.search?.marks.map((item) => (
              <li key={item.id}>
                {item.label}
                <span className="mono">
                  {' '}
                  {item.level} dBm · {hz(item.frequency_hz)}
                </span>
              </li>
            ))}
          </ol>
        </Panel>
      </div>
    </div>
  );
}
