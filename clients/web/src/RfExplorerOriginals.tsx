import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, type Api } from './api';
import { Spectrum } from './charts';
import { utc } from './components';
import { t } from './i18n';
import { prepareSweep } from './pipeline';
import type { PassWindow, WindowPage } from './rf-pass';
import type { Sweep } from './types';
import { message } from './useDesk';
import { encodePathSegment } from './wire';

export type OpenRfOriginal = (source: Sweep, pointIndex?: number) => void;

export async function readRfOriginal(
  api: Api,
  session: string,
  stream: string,
  id: string,
  signal: AbortSignal,
): Promise<Sweep> {
  const source = await api.get<Sweep>(
    '/sessions/' + encodePathSegment(session) + '/sweeps/' + encodePathSegment(id),
    signal,
  );
  if (
    source.session_id !== session ||
    source.stream_id !== stream ||
    source.id !== id ||
    source.level_unit !== 'dBm'
  )
    throw Error('Source evidence mismatch');
  return source;
}

/** Archive refreshes have one in-flight GET per resource and never depend on live frame count. */
export function useArchiveRead<T>(
  api: Api,
  path: string | undefined,
  pollMs = 0,
  paused = false,
  revision = 0,
  complete?: (value: T) => boolean,
) {
  const [state, setState] = useState<{ path: string; value?: T; error?: string; code?: string }>();
  const lastSuccess = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!path) return;
    const token = JSON.stringify([path, revision]);
    const url = path,
      controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      let finished = false;
      try {
        const value = await api.get<T>(url, controller.signal);
        if (!controller.signal.aborted) {
          finished = complete?.(value) ?? false;
          lastSuccess.current = token;
          setState({ path: url, value });
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setState((previous) => ({
            path: url,
            value: previous?.path === url ? previous.value : undefined,
            error: message(cause),
            code: cause instanceof ApiError ? cause.code : undefined,
          }));
      } finally {
        if (!controller.signal.aborted && pollMs > 0 && !paused && !finished)
          timer = setTimeout(read, pollMs);
      }
    }
    if (!paused || lastSuccess.current !== token) void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, path, pollMs, paused, revision, complete]);
  return state?.path === path ? state : undefined;
}

export function RfOriginalPlayback({
  api,
  session,
  stream,
  passPath,
  paused,
  openOriginal,
}: {
  api: Api;
  session: string;
  stream: string;
  passPath: string;
  paused: boolean;
  openOriginal: OpenRfOriginal;
}) {
  const [windows, setWindows] = useState<PassWindow[]>([]),
    [after, setAfter] = useState(0);
  const [nextCursor, setNextCursor] = useState<number | null>(null),
    [index, setIndex] = useState(0);
  const [source, setSource] = useState<Sweep>(),
    [error, setError] = useState('');
  const [playing, setPlaying] = useState(false),
    [retry, setRetry] = useState(0);
  useEffect(() => {
    if (paused) setPlaying(false);
  }, [paused]);
  const anchor = useRef({ recorded: 0, wall: 0 });
  const cache = useRef(new Map<string, Sweep>());
  const current = windows[index],
    next = windows[index + 1];
  useEffect(() => {
    const controller = new AbortController();
    void api
      .get<WindowPage>(
        passPath + '/windows?' + new URLSearchParams({ after_tile: String(after), limit: '100' }),
        controller.signal,
      )
      .then((page) => {
        if (controller.signal.aborted) return;
        if (page.items.some((w) => w.tile <= after))
          throw Error('Invalid RF Explorer pass response');
        setWindows((previous) => [...previous.filter((w) => w.tile <= after), ...page.items]);
        setNextCursor(page.next_after_tile);
        setError('');
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(message(cause));
          setPlaying(false);
        }
      });
    return () => controller.abort();
  }, [api, passPath, after, retry]);

  // Fetch only the next page of references when approaching the end of the loaded page.
  useEffect(() => {
    if (nextCursor !== null && index >= windows.length - 2) setAfter(nextCursor);
  }, [nextCursor, index, windows.length]);

  useEffect(() => {
    if (!current) return;
    const controller = new AbortController(),
      id = current.sweep_id;
    const cached = cache.current.get(id);
    if (cached) setSource(cached);
    else
      void readRfOriginal(api, session, stream, id, controller.signal)
        .then((value) => {
          if (controller.signal.aborted) return;
          if (
            value.interval.started_at !== current.started_at ||
            value.interval.ended_at !== current.ended_at
          )
            throw Error('Source evidence mismatch');
          setSource(value);
          setError('');
          cache.current.set(id, value);
          if (cache.current.size > 3) cache.current.delete(cache.current.keys().next().value!);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) {
            setError(message(cause));
            setPlaying(false);
          }
        });
    return () => controller.abort();
  }, [api, session, stream, current, retry]);

  const shown = source?.id === current?.sweep_id ? source : undefined;
  useEffect(() => {
    if (!playing || !shown) return;
    if (!next) {
      if (nextCursor === null) setPlaying(false);
      return;
    }
    // Preserve original gaps and use a common monotonic clock across page/network delays.
    const due = anchor.current.wall + Date.parse(next.started_at) - anchor.current.recorded;
    let timer: ReturnType<typeof setTimeout>;
    const advance = () => {
      const remaining = due - performance.now();
      if (remaining > 0) timer = setTimeout(advance, Math.min(remaining, 2_147_483_646));
      else setIndex((i) => i + 1);
    };
    advance();
    return () => clearTimeout(timer);
  }, [playing, shown, next, nextCursor]);
  const display = useMemo(() => shown && prepareSweep(shown).sweep, [shown]);
  return (
    <div role="group" aria-label={t('Original window playback')} className="rf-review">
      <h3>{t('Original window playback')}</h3>
      <div className="actions">
        <button
          disabled={index === 0}
          onClick={() => {
            setPlaying(false);
            setIndex((i) => i - 1);
          }}
        >
          {t('Previous sweep')}
        </button>
        <button
          disabled={!next}
          onClick={() => {
            setPlaying(false);
            setIndex((i) => i + 1);
          }}
        >
          {t('Next sweep')}
        </button>
        <button
          disabled={!playing && (!shown || !next)}
          onClick={() => {
            if (!playing && shown)
              anchor.current = {
                recorded: Date.parse(shown.interval.started_at),
                wall: performance.now(),
              };
            setPlaying((p) => !p);
          }}
        >
          {t(playing ? 'Pause playback' : 'Play at recorded speed')}
        </button>
      </div>
      {error && (
        <p role="alert" className="error-text">
          {t(error)} <button onClick={() => setRetry((r) => r + 1)}>{t('Retry')}</button>
        </p>
      )}
      {current && (
        <p className="muted break">
          {t('Window {tile}', { tile: current.tile })} · {current.sweep_id}
          <br />
          {utc(current.started_at)} → {utc(current.ended_at)}
        </p>
      )}
      {display && <Spectrum sweep={display} />}
      {shown && <button onClick={() => openOriginal(shown)}>{t('Open original sweep')}</button>}
    </div>
  );
}
