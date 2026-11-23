import { useEffect, useState } from 'react';
import type {
  FrequencyBookmarkPage,
  FrequencyBookmarkSet,
} from '../../../contracts/generated/typescript/index.ts';
import type { Api } from './api';
import { hz } from './components';
import { t } from './i18n';
import type { PassPoint } from './rf-pass';
import { message } from './useDesk';
import { encodePathSegment } from './wire';

type Marker = { frequency_hz: number; label: string };

export function RfPassMarkers({
  api,
  session,
  point,
  readOnly,
  select,
}: {
  api: Api;
  session: string;
  point: PassPoint | null;
  readOnly: boolean;
  select: (frequency: number) => void;
}) {
  const [items, setItems] = useState<FrequencyBookmarkSet[]>([]);
  const [name, setName] = useState(''),
    [chosen, setChosen] = useState('');
  const [draft, setDraft] = useState<Marker[] | null>(null);
  const [saving, setSaving] = useState(false),
    [error, setError] = useState('');
  const path = '/sessions/' + encodePathSegment(session) + '/frequency-bookmarks';
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .get<FrequencyBookmarkPage>(path, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted) {
          if (page.session_id !== session || page.items.some((m) => m.session_id !== session))
            throw Error('Source evidence mismatch');
          setItems(page.items);
          setError('');
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(message(e));
      });
    return () => controller.abort();
  }, [api, path, session, revision]);
  const rows = draft ?? [];
  const canAdd =
    !!point && rows.length < 4 && !rows.some((entry) => entry.frequency_hz === point.frequency_hz);
  const title = name.trim();
  const entries =
    draft === null
      ? point
        ? [{ label: title, frequency_hz: point.frequency_hz }]
        : []
      : draft.map((entry) => ({ ...entry, label: entry.label.trim() }));
  const canSave = !!title && entries.length > 0 && entries.every((entry) => entry.label.length > 0);

  async function store() {
    if (readOnly || saving || !canSave) return;
    setSaving(true);
    try {
      const saved = await api.post<FrequencyBookmarkSet>(path, { name: title, entries });
      if (saved.session_id !== session) throw Error('Source evidence mismatch');
      const index = saved.entries.findIndex(
        (entry) => Number(entry.frequency_hz) === point?.frequency_hz,
      );
      setChosen(index >= 0 ? saved.id + '/' + index : '');
      setName('');
      setDraft(null);
      setRevision((r) => r + 1);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setSaving(false);
    }
  }
  const markers = items.flatMap((set) =>
    set.entries.map((entry, i) => ({ id: set.id + '/' + i, set, entry })),
  );
  const selectedSet = markers.find((marker) => marker.id === chosen)?.set;
  return (
    <div className="rf-markers">
      <label>
        {t('Saved frequency markers')}
        <select
          value={chosen}
          onChange={(e) => {
            const match = markers.find((m) => m.id === e.target.value);
            setChosen(e.target.value);
            if (match) select(Number(match.entry.frequency_hz));
          }}
        >
          <option value="">{t('Select a frequency')}</option>
          {markers.map(({ id, set, entry }) => (
            <option value={id} key={id}>
              {set.name}
              {entry.label === set.name ? '' : ' · ' + entry.label} · {hz(entry.frequency_hz)} ·{' '}
              {t('Revision {number}', { number: set.revision })}
            </option>
          ))}
        </select>
      </label>
      {!readOnly && (
        <>
          <div className="actions rf-inline-form">
            <label>
              {t(draft === null ? 'Marker name' : 'Marker set name')}
              <input
                maxLength={80}
                value={name}
                disabled={saving}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <button
              type="button"
              disabled={saving || !canAdd}
              onClick={() => {
                if (point && canAdd)
                  setDraft([
                    ...rows,
                    { label: hz(point.frequency_hz), frequency_hz: point.frequency_hz },
                  ]);
              }}
            >
              {t('Add selected frequency')}
            </button>
            {selectedSet && draft === null && (
              <button
                type="button"
                disabled={saving}
                onClick={() => {
                  setName(selectedSet.name);
                  setDraft(
                    selectedSet.entries.map((entry) => ({
                      label: entry.label,
                      frequency_hz: Number(entry.frequency_hz),
                    })),
                  );
                }}
              >
                {t('Edit marker set')}
              </button>
            )}
          </div>
          {rows.map((entry) => (
            <div className="actions rf-inline-form" key={entry.frequency_hz}>
              <label>
                {t('Label for {frequency}', { frequency: hz(entry.frequency_hz) })}
                <input
                  maxLength={80}
                  value={entry.label}
                  disabled={saving}
                  onChange={(e) =>
                    setDraft(
                      rows.map((row) => (row === entry ? { ...row, label: e.target.value } : row)),
                    )
                  }
                />
              </label>
              <button
                type="button"
                disabled={saving}
                aria-label={t('Remove {frequency}', { frequency: hz(entry.frequency_hz) })}
                onClick={() => setDraft(rows.filter((row) => row !== entry))}
              >
                {t('Remove')}
              </button>
            </div>
          ))}
          <div className="actions">
            <button type="button" disabled={saving || !canSave} onClick={() => void store()}>
              {t(draft === null ? 'Save marker' : 'Save marker set')}
            </button>
            {draft !== null && (
              <button
                type="button"
                disabled={saving}
                onClick={() => {
                  setDraft(null);
                  setName('');
                }}
              >
                {t('Cancel')}
              </button>
            )}
          </div>
        </>
      )}
      {error && (
        <p className="error-text" role="alert">
          {t(error)}
        </p>
      )}
    </div>
  );
}
