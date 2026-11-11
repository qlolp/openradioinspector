import { t } from './i18n';
import { useRef, useState } from 'react';
import { receiveSession } from './api';
import type { Desk } from './useDesk';
import type { Project, Session } from './types';
import { Modal } from './components';

export function Prepare({ desk, close }: { desk: Desk; close: () => void }) {
  const [step, setStep] = useState(0),
    [error, setError] = useState('');
  const [form, setForm] = useState({
    name: '',
    site: '',
    simulator: false,
    analyzerPort: '',
    receiverPort: '',
    startHz: 88000000,
    stopHz: 108000000,
    points: 201,
    rbwHz: 10000,
    notes: '',
    receive: false,
    location: false,
    session: '',
  });
  const created = useRef<Project | undefined>(undefined);
  const update = (key: keyof typeof form, value: string | number | boolean) =>
    setForm((previous) => ({ ...previous, [key]: value }));
  async function create() {
    setError('');
    if (!form.notes.trim() || !form.receive || !form.location) {
      setError('Confirm the receive path, antenna, location, and ordinary environment');
      return;
    }
    const result = await desk.run(
      'Prepare receive-only project',
      async () => {
        let project = created.current;
        if (!project) {
          const session = form.session
            ? desk.sessions.find((s) => s.id === form.session)!
            : await desk.api.post<Session>('/sessions', receiveSession(form));
          project = await desk.api.post<Project>('/monitoring-projects', {
            name: form.name,
            site: form.site,
            session_id: session.id,
          });
          created.current = project;
        }
        await desk.api.project(project.id, 'preparation', {
          antenna_notes: form.notes,
          receive_path_confirmed: form.receive,
          location_confirmed: form.location,
        });
        await desk.loadProjects('', [], project);
        return project;
      },
      [],
    );
    if (result) close();
  }
  return (
    <Modal label={t('Prepare a receive-only project')} close={close} wide>
      <div className="eyebrow">
        {t('NEW PROJECT ·') + ' '}
        {step + 1} / 2
      </div>
      <h2>{t('Prepare a receive-only project')}</h2>
      <p className="muted">
        {t(
          'The core owns both instruments. Preparation saves your choices; collection starts only when you explicitly request it.',
        )}
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (step === 0) {
            try {
              if (!form.session) receiveSession(form);
              setError('');
              setStep(1);
            } catch (cause) {
              setError((cause as Error).message);
            }
          } else void create();
        }}
      >
        {step === 0 ? (
          <>
            <div className="form-grid">
              <label>
                {t('Project name')}
                <input
                  required
                  value={form.name}
                  onChange={(e) => update('name', e.target.value)}
                  maxLength={200}
                />
              </label>
              <label>
                {t('Site or location')}
                <input
                  required
                  value={form.site}
                  onChange={(e) => update('site', e.target.value)}
                  maxLength={200}
                />
              </label>
            </div>
            <label>
              {t('Receive session')}
              <select value={form.session} onChange={(e) => update('session', e.target.value)}>
                <option value="">{t('Create a new two-instrument session')}</option>
                {desk.sessions
                  .filter(
                    (s) =>
                      s.streams.filter((x) => x.role === 'spectrum_analyzer').length === 1 &&
                      s.streams.filter((x) => x.role === 'receiver').length === 1,
                  )
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} · {s.state}
                    </option>
                  ))}
              </select>
            </label>
            {!form.session && (
              <>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={form.simulator}
                    onChange={(e) => update('simulator', e.target.checked)}
                  />
                  {t('Use explicit simulators')}
                </label>
                <p className="callout">
                  {form.simulator
                    ? t('Synthetic receive data. This does not validate hardware.')
                    : t(
                        'Conservative tinySA receive profile: 100 kHz–800 MHz, 2–290 points. No generator controls.',
                      )}
                </p>
                {!form.simulator && (
                  <div className="form-grid">
                    <label>
                      {t('tinySA serial port')}
                      <input
                        required
                        placeholder={t('/dev/ttyACM0 or COM3')}
                        value={form.analyzerPort}
                        onChange={(e) => update('analyzerPort', e.target.value)}
                      />
                    </label>
                    <label>
                      {t('AR-DV1 serial port')}
                      <input
                        required
                        placeholder={t('/dev/ttyUSB0 or COM4')}
                        value={form.receiverPort}
                        onChange={(e) => update('receiverPort', e.target.value)}
                      />
                    </label>
                  </div>
                )}
                <div className="form-grid">
                  <label>
                    {t('Start frequency (Hz)')}
                    <input
                      type="number"
                      min={100000}
                      max={800000000}
                      step={1}
                      required
                      value={form.startHz}
                      onChange={(e) => update('startHz', Number(e.target.value))}
                    />
                  </label>
                  <label>
                    {t('Stop frequency (Hz)')}
                    <input
                      type="number"
                      min={100000}
                      max={800000000}
                      step={1}
                      required
                      value={form.stopHz}
                      onChange={(e) => update('stopHz', Number(e.target.value))}
                    />
                  </label>
                  <label>
                    {t('Points')}
                    <input
                      type="number"
                      min={2}
                      max={290}
                      step={1}
                      required
                      value={form.points}
                      onChange={(e) => update('points', Number(e.target.value))}
                    />
                  </label>
                  <label>
                    {t('Requested RBW')}
                    <select
                      value={form.rbwHz}
                      onChange={(e) => update('rbwHz', Number(e.target.value))}
                    >
                      <option value={10000}>{t('10 kHz')}</option>
                      <option value={30000}>{t('30 kHz')}</option>
                    </select>
                  </label>
                </div>
              </>
            )}
          </>
        ) : (
          <>
            <div className="callout">
              <strong>{form.name}</strong> · {form.site}
              <br />
              {form.session
                ? t('Existing core session')
                : form.simulator
                  ? t('Explicit synthetic profiles')
                  : t('tinySA Ultra + AR-DV1 · receive only')}
            </div>
            <label>
              {t('Antenna and receive path notes')}
              <textarea
                required
                maxLength={1000}
                rows={3}
                value={form.notes}
                onChange={(e) => update('notes', e.target.value)}
              />
            </label>
            <label className="check">
              <input
                type="checkbox"
                required
                checked={form.receive}
                onChange={(e) => update('receive', e.target.checked)}
              />
              {t('Receive path and antenna checked')}
            </label>
            <label className="check">
              <input
                type="checkbox"
                required
                checked={form.location}
                onChange={(e) => update('location', e.target.checked)}
              />
              {t('Location and ordinary RF environment checked')}
            </label>
            <p className="muted">
              {t(
                'After creating the project, run preflight, collect at least 20 sweeps over 60 seconds, then confirm a baseline before arming. RF readback and hardware validation remain unverified.',
              )}
            </p>
          </>
        )}
        {error && (
          <p role="alert" className="error-text">
            {t(error)}
          </p>
        )}
        {desk.action.state === 'Error' && (
          <p role="alert" className="error-text">
            {t(desk.action.text)}
          </p>
        )}
        <div className="actions">
          {step === 1 && !created.current && (
            <button type="button" onClick={() => setStep(0)}>
              {t('Back')}
            </button>
          )}
          <button className="primary" disabled={desk.busy}>
            {step === 0 ? t('Next: receive path') : t('Create prepared project')}
          </button>
          <button type="button" disabled={desk.busy} onClick={close}>
            {t('Cancel')}
          </button>
        </div>
      </form>
    </Modal>
  );
}
