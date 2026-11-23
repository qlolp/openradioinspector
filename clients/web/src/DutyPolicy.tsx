import { useState } from 'react';
import { t } from './i18n';
import { utc } from './components';
import { useArchiveRead } from './RfExplorerOriginals';
import { encodePathSegment } from './wire';
import type { Desk } from './useDesk';

type Schedule =
  | { mode: 'continuous' }
  | { mode: 'until'; until_at: string }
  | {
      mode: 'weekly';
      timezone: string;
      weekdays: number[];
      start_local: string;
      end_local: string;
    };
type Duty = {
  enabled: boolean;
  status: string;
  last_error?: string;
  schedule: Schedule;
  resume_after_restart: boolean;
  deliveries?: {
    id: string;
    channel: string;
    kind: string;
    state: string;
    attempts: number;
    next_at?: string;
    error?: string;
    created_at: string;
  }[];
};
type Channel = 'webhook' | 'telegram';
type Setting = { enabled: boolean; configured: boolean };
function NotificationChannel({
  channel,
  saved,
  desk,
  reload,
}: {
  channel: Channel;
  saved?: Setting;
  desk: Desk;
  reload: () => void;
}) {
  const [enabledEdit, setEnabled] = useState<boolean>(),
    [endpoint, setEndpoint] = useState(''),
    [secret, setSecret] = useState(''),
    [receipt, setReceipt] = useState<{ notification_id: string; state: string }>();
  const enabled = enabledEdit ?? saved?.enabled ?? false;
  const name = channel === 'webhook' ? 'Webhook' : 'Telegram';
  return (
    <details>
      <summary>
        {name} · {t(saved?.enabled ? 'Enabled' : 'Disabled')}
      </summary>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const details = enabled
            ? channel === 'telegram'
              ? { ...(endpoint && { chat_id: endpoint }), ...(secret && { bot_token: secret }) }
              : { ...(endpoint && { url: endpoint }), ...(secret && { secret }) }
            : {};
          const result = await desk.command(
            'settings',
            { notifications: { [channel]: { enabled, ...details } } },
            [],
          );
          if (result) {
            setSecret('');
            setEndpoint('');
            reload();
          }
        }}
      >
        <label className="check">
          <input
            type="checkbox"
            disabled={!saved || desk.busy}
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          {t('Enable {channel} for this project', { channel: name })}
        </label>
        {enabled && (
          <>
            <label>
              {t(channel === 'telegram' ? 'Telegram chat ID' : 'Webhook URL')}
              <input
                autoComplete="off"
                required={!saved?.configured}
                type={channel === 'telegram' ? 'text' : 'url'}
                placeholder={saved?.configured ? t('Keep stored setting') : ''}
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
              />
            </label>
            <label>
              {t(channel === 'telegram' ? 'Telegram bot token' : 'Webhook secret')}
              <input
                type="password"
                autoComplete="new-password"
                required={channel === 'telegram' && !saved?.configured}
                value={secret}
                placeholder={saved?.configured ? t('Keep stored setting') : ''}
                onChange={(e) => setSecret(e.target.value)}
              />
            </label>
          </>
        )}
        <div className="actions">
          <button disabled={desk.busy || !saved}>{t('Save channel')}</button>
          <button
            type="button"
            disabled={desk.busy || !saved?.enabled || !saved.configured}
            onClick={async () => {
              const sent = await desk.run(
                'Send test notification',
                () =>
                  desk.api.project<{ notification_id: string; state: string }>(
                    desk.projectId,
                    'notifications/test',
                    { channel },
                  ),
                [],
              );
              if (sent) {
                setReceipt(sent);
                reload();
              }
            }}
          >
            {t('Send test notification')}
          </button>
        </div>
        {receipt && (
          <p role="status">
            {t('Test notification')} · {t(receipt.state)} · <code>{receipt.notification_id}</code>
          </p>
        )}
      </form>
    </details>
  );
}

export function DutyPolicy({ desk, onArmed }: { desk: Desk; onArmed?: () => void }) {
  const flags = desk.capabilities?.features,
    scheduleAvailable = flags?.monitoring_duty_schedule === true,
    notifyAvailable = flags?.monitoring_notification_channels === true;
  const prefix = '/monitoring-projects/' + encodePathSegment(desk.projectId);
  const [rev, setRev] = useState(0);
  const policy = useArchiveRead<Duty>(
    desk.api,
    scheduleAvailable ? prefix + '/duty' : undefined,
    5000,
    false,
    rev,
  );
  const settings = useArchiveRead<{ notifications?: Partial<Record<Channel, Setting>> }>(
    desk.api,
    notifyAvailable ? prefix + '/settings' : undefined,
    0,
    false,
    rev,
  );
  const current = policy?.value,
    stored = current?.schedule,
    weekly = stored?.mode === 'weekly' ? stored : undefined;
  const [modeEdit, setMode] = useState<Schedule['mode']>(),
    [untilEdit, setUntil] = useState<string>(),
    [zoneEdit, setZone] = useState<string>(),
    [daysEdit, setDays] = useState<number[]>(),
    [startEdit, setStart] = useState<string>(),
    [endEdit, setEnd] = useState<string>(),
    [resumeEdit, setResume] = useState<boolean>(),
    [error, setError] = useState('');
  const mode = modeEdit ?? stored?.mode ?? 'continuous',
    until = untilEdit ?? (stored?.mode === 'until' ? stored.until_at.substring(0, 16) : ''),
    zone =
      zoneEdit ?? weekly?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC',
    days = daysEdit ?? weekly?.weekdays ?? [1, 2, 3, 4, 5],
    start = startEdit ?? weekly?.start_local ?? '08:00',
    end = endEdit ?? weekly?.end_local ?? '18:00',
    resume = resumeEdit ?? current?.resume_after_restart ?? false;
  async function arm() {
    setError('');
    let schedule: Schedule = { mode: 'continuous' };
    if (mode === 'until') {
      const at = new Date(until + 'Z');
      if (!Number.isFinite(at.valueOf()) || at.valueOf() <= Date.now()) {
        setError('Choose a future UTC end time.');
        return;
      }
      schedule = { mode, until_at: at.toISOString() };
    }
    if (mode === 'weekly') {
      try {
        new Intl.DateTimeFormat('en', { timeZone: zone }).format();
      } catch {
        setError('Enter an IANA time zone, such as Europe/Moscow.');
        return;
      }
      if (!zone.trim() || !days.length || start === end) {
        setError('Choose weekdays, a time zone and a window with different start and end times.');
        return;
      }
      schedule = {
        mode,
        timezone: zone.trim(),
        weekdays: [...days].sort(),
        start_local: start,
        end_local: end,
      };
    }
    if (!scheduleAvailable) {
      if (await desk.command('arm')) onArmed?.();
      return;
    }
    const response = await desk.run('Arm duty', () =>
      desk.api.project<Duty>(desk.projectId, 'duty', { schedule, resume_after_restart: resume }),
    );
    if (response) {
      setRev((n) => n + 1);
      if (response.enabled && ['armed', 'waiting'].includes(response.status)) onArmed?.();
      else setError(response.last_error || response.status);
    }
  }
  return (
    <div className="duty-policy">
      {notifyAvailable && (
        <>
          <h3>{t('Notification channels')}</h3>
          {(['telegram', 'webhook'] as const).map((ch) => (
            <NotificationChannel
              key={ch}
              channel={ch}
              saved={settings?.value?.notifications?.[ch]}
              desk={desk}
              reload={() => setRev((n) => n + 1)}
            />
          ))}
          {settings?.error && <p role="alert">{t(settings.error)}</p>}
        </>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void arm();
        }}
      >
        {scheduleAvailable && (
          <>
            <label>
              {t('Duty schedule')}
              <select
                disabled={!current || desk.busy}
                value={mode}
                onChange={(e) => setMode(e.target.value as typeof mode)}
              >
                <option value="continuous">{t('Continuous')}</option>
                <option value="until">{t('Until a UTC time')}</option>
                <option value="weekly">{t('Weekly time window')}</option>
              </select>
            </label>
            {mode === 'until' && (
              <label>
                {t('End time (UTC)')}
                <input
                  type="datetime-local"
                  required
                  value={until}
                  onChange={(e) => setUntil(e.target.value)}
                />
              </label>
            )}
            {mode === 'weekly' && (
              <>
                <div className="form-grid">
                  <label>
                    {t('Time zone')}
                    <input required value={zone} onChange={(e) => setZone(e.target.value)} />
                  </label>
                  <label>
                    {t('Local start time')}
                    <input
                      type="time"
                      required
                      value={start}
                      onChange={(e) => setStart(e.target.value)}
                    />
                  </label>
                  <label>
                    {t('Local end time')}
                    <input
                      required
                      type="time"
                      value={end}
                      onChange={(e) => setEnd(e.target.value)}
                    />
                  </label>
                </div>
                <fieldset className="actions">
                  <legend>{t('Weekdays')}</legend>
                  {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((label, i) => (
                    <label key={label} className="check">
                      <input
                        type="checkbox"
                        checked={days.includes(i + 1)}
                        onChange={(e) =>
                          setDays(
                            e.target.checked ? [...days, i + 1] : days.filter((d) => d !== i + 1),
                          )
                        }
                      />
                      {t(label)}
                    </label>
                  ))}
                </fieldset>
              </>
            )}
            <label className="check">
              <input
                type="checkbox"
                disabled={!current || desk.busy}
                checked={resume}
                onChange={(e) => setResume(e.target.checked)}
              />
              {t('Resume duty after a node restart and a successful preflight')}
            </label>
            {current?.status && (
              <p>
                {t('Duty status')} · {t(current.status)} {current.last_error}
              </p>
            )}
          </>
        )}
        {(error || policy?.error) && (
          <p role="alert" className="error-text">
            {t(error || policy!.error!)}
          </p>
        )}
        <div className="actions">
          <button
            className="primary"
            disabled={
              desk.busy ||
              !desk.preflight?.ready ||
              !desk.preflight?.baseline_ready ||
              !!desk.guard?.active ||
              (scheduleAvailable && !current)
            }
          >
            {t(scheduleAvailable ? 'Arm duty' : 'Arm guard')}
          </button>
          {current?.enabled && (
            <button
              type="button"
              className="danger"
              disabled={desk.busy}
              onClick={async () => {
                if (await desk.command('disarm')) setRev((n) => n + 1);
              }}
            >
              {t('Stop duty')}
            </button>
          )}
        </div>
      </form>
      {!!current?.deliveries?.length && (
        <details>
          <summary>{t('Notification delivery history')}</summary>
          <ul>
            {current.deliveries.map((d) => (
              <li key={d.id}>
                {d.channel} · {t(d.state)} · {d.kind} · {utc(d.created_at)} · {t('Attempts')}{' '}
                {d.attempts} {d.error}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
