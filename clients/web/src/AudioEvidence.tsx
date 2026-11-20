import { useEffect, useRef, useState } from 'react';
import type { Api } from './api';
import { t, useLocale } from './i18n';
import { encodePathSegment } from './wire';

interface Info {
  artifact_id: string;
  sha256: string;
  frame_count: number;
  sample_rate_hz: number;
}
interface Mark {
  id: string;
  label: string;
  start_frame: number;
  end_frame: number;
  source_artifact_id: string;
  source_sha256: string;
}
interface Page {
  items: Mark[];
  next_cursor: string | null;
}
export function AudioEvidence({
  api,
  projectId,
  eventId,
  available,
  audio,
}: {
  api: Api;
  projectId: string;
  eventId: string;
  available: boolean;
  audio: (path: string, signal?: AbortSignal) => Promise<Blob>;
}) {
  useLocale();
  const [info, setInfo] = useState<Info>(),
    [page, setPage] = useState<Page>({ items: [], next_cursor: null });
  const [start, setStart] = useState('0'),
    [end, setEnd] = useState(''),
    [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('');
  const scope = useRef<AbortController>(undefined);
  const base = `/monitoring-projects/${encodePathSegment(projectId)}/events/${encodePathSegment(eventId)}/audio`;
  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    setInfo(undefined);
    setPage({ items: [], next_cursor: null });
    setStart('0');
    setEnd('');
    setLabel('');
    setError('');
    setNotice('');
    setBusy(available);
    if (available)
      void Promise.all([
        api.get<Info>(base + '/info', controller.signal),
        api.get<Page>(base + '/marks?limit=32', controller.signal),
      ])
        .then(([value, marks]) => {
          if (controller.signal.aborted || scope.current !== controller) return;
          if (
            !Number.isSafeInteger(value.frame_count) ||
            value.frame_count < 1 ||
            value.frame_count > 8388608 ||
            !Number.isSafeInteger(value.sample_rate_hz) ||
            value.sample_rate_hz < 1 ||
            value.sample_rate_hz > 192000 ||
            !/^[a-f0-9]{64}$/.test(value.sha256) ||
            typeof value.artifact_id !== 'string'
          )
            throw Error('Invalid audio source');
          setInfo(value);
          setEnd(String(value.frame_count));
          setPage(marks);
        })
        .catch((failure: unknown) => {
          if (!controller.signal.aborted && scope.current === controller)
            setError(failure instanceof Error ? failure.message : String(failure));
        })
        .finally(() => {
          if (!controller.signal.aborted && scope.current === controller) setBusy(false);
        });
    return () => controller.abort();
  }, [api, base, available]);
  const valid =
    !!info &&
    /^\d+$/.test(start) &&
    /^\d+$/.test(end) &&
    Number.isSafeInteger(Number(start)) &&
    Number.isSafeInteger(Number(end)) &&
    Number(start) < Number(end) &&
    Number(end) <= info.frame_count;
  async function perform(work: (signal: AbortSignal) => Promise<void>) {
    const controller = scope.current;
    if (!controller || controller.signal.aborted || busy) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await work(controller.signal);
    } catch (failure) {
      if (!controller.signal.aborted && scope.current === controller)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted && scope.current === controller) setBusy(false);
    }
  }
  async function marks(after?: string) {
    await perform(async (signal) => {
      const value = await api.get<Page>(
        base + '/marks?limit=32' + (after ? '&after=' + encodeURIComponent(after) : ''),
        signal,
      );
      if (!signal.aborted) setPage(value);
    });
  }
  async function save() {
    if (!valid || !label.trim() || !info) return;
    await perform(async (signal) => {
      await api.post(
        base + '/marks',
        {
          start_frame: Number(start),
          end_frame: Number(end),
          label: label.trim(),
          source_artifact_id: info.artifact_id,
          source_sha256: info.sha256,
        },
        signal,
      );
      const value = await api.get<Page>(base + '/marks?limit=32', signal);
      if (!signal.aborted) {
        setPage(value);
        setNotice('Audio mark saved');
        setLabel('');
      }
    });
  }
  async function download() {
    if (!valid || !info) return;
    await perform(async (signal) => {
      const blob = await audio(
        '/api/v1' +
          base +
          `/clip?start_frame=${Number(start)}&end_frame=${Number(end)}&source_artifact_id=${encodeURIComponent(info.artifact_id)}&source_sha256=${info.sha256}`,
        signal,
      );
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob),
        anchor = document.createElement('a');
      try {
        anchor.href = url;
        anchor.download = 'ori-audio-clip.wav';
        anchor.click();
        setNotice('Audio fragment exported');
      } finally {
        URL.revokeObjectURL(url);
      }
    });
  }
  if (!available) return null;
  return (
    <section aria-label={t('Audio fragments and marks')}>
      <h3>{t('Audio fragments and marks')}</h3>
      <p className="muted">
        {t(
          'Exports preserve original samples and include source identity. The original recording stays unchanged.',
        )}
      </p>
      {info && (
        <p>
          {info.frame_count} {t('samples')} · {info.sample_rate_hz} Hz ·{' '}
          {(info.frame_count / info.sample_rate_hz).toFixed(3)} {t('s')}
        </p>
      )}
      {info && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label>
            {t('First sample')}
            <input
              name="start"
              type="number"
              min={0}
              max={info.frame_count - 1}
              step={1}
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </label>
          <label>
            {t('End sample (excluded)')}
            <input
              name="end"
              type="number"
              min={1}
              max={info.frame_count}
              step={1}
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </label>
          {valid && (
            <p className="mono">
              {(Number(start) / info.sample_rate_hz).toFixed(3)}…
              {(Number(end) / info.sample_rate_hz).toFixed(3)} {t('s')}
            </p>
          )}
          <label>
            {t('Mark label')}
            <input
              name="label"
              value={label}
              maxLength={200}
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
          <div className="actions">
            <button type="submit" disabled={busy || !valid || !label.trim()}>
              {t('Save audio mark')}
            </button>
            <button
              type="button"
              data-export-audio
              disabled={busy || !valid}
              onClick={() => void download()}
            >
              {t('Export WAV fragment')}
            </button>
          </div>
        </form>
      )}
      <ul className="marks">
        {page.items.map((mark) => (
          <li key={mark.id}>
            <button
              type="button"
              disabled={
                busy ||
                mark.source_artifact_id !== info?.artifact_id ||
                mark.source_sha256 !== info?.sha256
              }
              onClick={() => {
                setStart(String(mark.start_frame));
                setEnd(String(mark.end_frame));
              }}
            >
              {mark.label} · {mark.start_frame}…{mark.end_frame}
            </button>
          </li>
        ))}
      </ul>
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => void marks()}>
          {t('First marks page')}
        </button>
        <button
          type="button"
          disabled={busy || !page.next_cursor}
          onClick={() => void marks(page.next_cursor!)}
        >
          {t('Next marks page')}
        </button>
      </div>
      {busy && <p role="status">{t('Loading or saving audio evidence')}</p>}
      {notice && <p role="status">{t(notice)}</p>}
      {error && <p role="alert">{t(error)}</p>}
    </section>
  );
}
