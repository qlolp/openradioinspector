import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Api, ResourceScope } from './api';
import { LiveStore } from './live';
import type { Connection } from './transport';
import type {
  Allowed,
  Capabilities,
  GuardStatus,
  Health,
  MonitoringEvent,
  Observation,
  Page,
  Preflight,
  Project,
  SearchStatus,
  Session,
  Sweep,
} from './types';

export function remember(key: string, fallback: string): string {
  try {
    return localStorage.getItem('ori-modern-' + key) || fallback;
  } catch {
    return fallback;
  }
}
export function save(key: string, value: string) {
  try {
    localStorage.setItem('ori-modern-' + key, value);
  } catch {
    /* Local storage is optional. */
  }
}
export const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function useDesk(transport: Connection) {
  const api = useMemo(() => new Api(transport), [transport]);
  const liveStore = useMemo(() => new LiveStore(transport), [transport]);
  const live = useSyncExternalStore(liveStore.subscribe, liveStore.getSnapshot);
  const [projects, setProjects] = useState<Project[]>([]),
    [sessions, setSessions] = useState<Session[]>([]);
  const [projectId, setProjectId] = useState(() => remember('project', ''));
  const [guard, setGuard] = useState<GuardStatus>(),
    [preflight, setPreflight] = useState<Preflight>();
  const [events, setEvents] = useState<MonitoringEvent[]>([]),
    [allowed, setAllowed] = useState<Allowed[]>([]),
    [search, setSearch] = useState<SearchStatus>();
  const [health, setHealth] = useState<Health>(),
    [capabilities, setCapabilities] = useState<Capabilities>();
  const [action, setAction] = useState({
    state: 'Ready',
    text: 'Receive-only workspace · core owns devices and archive',
  });
  const [busy, setBusy] = useState(false),
    [connected, setConnected] = useState(!transport.native);
  const [projectsCursor, setProjectsCursor] = useState<string | null>(null);
  const [projectsHistory, setProjectsHistory] = useState<string[]>([]);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const connectionEpoch = useRef(0);
  const selection = useRef(projectId);
  selection.current = projectId;
  const scope = useRef(new ResourceScope()),
    currentScope = useRef<{ generation: number; signal: AbortSignal } | null>(null);
  const running = useRef(false),
    mounted = useRef(true),
    projectAfter = useRef('');
  const project = guard?.project;

  const loadProjects = useCallback(
    async (after = '', history: string[] = [], pin?: Project) => {
      const page = await api.get<Page<Project>>(
        '/monitoring-projects?' +
          new URLSearchParams({ limit: '100', ...(after ? { after } : {}) }),
      );
      if (!mounted.current) return;
      setProjects((previous) => {
        const selected = pin || previous.find((p) => p.id === selection.current);
        return selected && !page.items.some((p) => p.id === selected.id)
          ? [selected, ...page.items]
          : page.items;
      });
      projectAfter.current = after;
      setProjectsHistory(history);
      setProjectsCursor(page.items.length === 100 ? page.next_cursor : null);
      if (pin) setProjectId(pin.id);
      else if (!selection.current && page.items[0]) setProjectId(page.items[0].id);
    },
    [api],
  );

  const refresh = useCallback(
    async (resources: string[] = ['status', 'events', 'preflight', 'allowed', 'search']) => {
      const id = selection.current,
        ticket = currentScope.current;
      if (!id || !ticket || ticket.signal.aborted) return;
      const valid = () =>
        scope.current.current(ticket.generation) && selection.current === id && mounted.current;
      const calls: Promise<void>[] = [];
      const read = <T>(path: string, commit: (value: T) => void) =>
        api.project<T>(id, path, undefined, ticket.signal).then((value) => {
          if (valid()) commit(value);
        });
      if (resources.includes('status')) calls.push(read<GuardStatus>('status', setGuard));
      if (resources.includes('events'))
        calls.push(
          read<Page<MonitoringEvent>>('events?limit=100', (page) => setEvents(page.items)),
        );
      if (resources.includes('preflight')) calls.push(read<Preflight>('preflight', setPreflight));
      if (resources.includes('allowed'))
        calls.push(read<Page<Allowed>>('allowed?limit=200', (page) => setAllowed(page.items)));
      if (resources.includes('search')) calls.push(read<SearchStatus>('search/status', setSearch));
      const results = await Promise.allSettled(calls);
      const failed = results.find((r) => r.status === 'rejected');
      if (valid() && failed?.status === 'rejected' && failed.reason?.name !== 'AbortError')
        setAction({ state: 'Error', text: message(failed.reason) });
    },
    [api],
  );

  const resyncRunning = useRef(false),
    resyncTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const seed = useCallback(
    async (p: Project, searchScope?: SearchStatus, signal?: AbortSignal) => {
      const sid =
        searchScope?.active && searchScope.session_id
          ? searchScope.session_id
          : p.active_session_id;
      const stream =
        searchScope?.active && searchScope.stream_id ? searchScope.stream_id : p.analyzer_stream_id;
      liveStore.scope(sid, stream, p.receiver_stream_id, p.active_session_id);
      const [sweeps, observations] = await Promise.all([
        api.get<Sweep[]>(
          `/sessions/${encodeURIComponent(sid)}/sweeps?` +
            new URLSearchParams({ stream_id: stream, limit: '1' }),
          signal,
        ),
        api.get<Observation[]>(
          `/sessions/${encodeURIComponent(p.active_session_id)}/observations?` +
            new URLSearchParams({ stream_id: p.receiver_stream_id, limit: '1' }),
          signal,
        ),
      ]);
      if (!signal?.aborted && selection.current === p.id) liveStore.seed(sweeps, observations);
    },
    [api, liveStore],
  );
  const projectRef = useRef(project),
    searchRef = useRef(search);
  projectRef.current = project;
  searchRef.current = search;
  const resync = useCallback(async () => {
    if (resyncRunning.current || !mounted.current) return;
    resyncRunning.current = true;
    const epoch = connectionEpoch.current;
    liveStore.stop();
    try {
      const value = await api.get<Health>('/health');
      if (!mounted.current || connectionEpoch.current !== epoch) return;
      setHealth(value);
      const p = projectRef.current;
      if (p) {
        try {
          await seed(p, searchRef.current, currentScope.current?.signal);
        } catch (error) {
          if ((error as Error).name !== 'AbortError') throw error;
        }
      }
      if (mounted.current && connectionEpoch.current === epoch)
        await liveStore.start(value.last_event_id);
      await refresh(['status', 'events']);
    } catch (error) {
      if (
        mounted.current &&
        connectionEpoch.current === epoch &&
        (error as Error).name !== 'AbortError'
      ) {
        setAction({ state: 'Error', text: 'Resynchronization failed: ' + message(error) });
        resyncTimer.current = setTimeout(() => {
          void resync();
        }, 5000);
      }
    } finally {
      resyncRunning.current = false;
    }
  }, [api, liveStore, seed, refresh]);

  const boot = useCallback(async () => {
    const epoch = connectionEpoch.current;
    try {
      const [h, c, s] = await Promise.all([
        api.get<Health>('/health'),
        api.get<Capabilities>('/capabilities'),
        api.get<Session[]>('/sessions'),
      ]);
      if (!mounted.current || connectionEpoch.current !== epoch) return;
      setHealth(h);
      setCapabilities(c);
      setSessions(s);
      await loadProjects();
      if (mounted.current && connectionEpoch.current === epoch)
        await liveStore.start(h.last_event_id);
    } catch (error) {
      if (connectionEpoch.current === epoch) throw error;
    }
  }, [api, liveStore, loadProjects]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      scope.current.dispose();
      clearTimeout(resyncTimer.current);
      liveStore.dispose();
    };
  }, [liveStore]);

  useEffect(() => {
    if (connected)
      void boot().catch((error) => {
        if (mounted.current) setAction({ state: 'Error', text: message(error) });
      });
  }, [connected, boot]);

  useEffect(() => {
    currentScope.current = scope.current.next();
    setGuard(undefined);
    setPreflight(undefined);
    setEvents([]);
    setAllowed([]);
    setSearch(undefined);
    liveStore.scope('', '', '');
    save('project', projectId);
    if (connected) void refresh();
  }, [projectId, connected, refresh, liveStore]);

  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    void seed(project, search, controller.signal).catch((error) => {
      if (!controller.signal.aborted) setAction({ state: 'Error', text: message(error) });
    });
    return () => controller.abort();
  }, [
    project?.id,
    project?.active_session_id,
    project?.analyzer_stream_id,
    search?.active,
    search?.session_id,
    seed,
  ]);

  useEffect(() => {
    if (!connected) return;
    let statusReading = false,
      eventsReading = false,
      healthReading = false;
    const statusTimer = setInterval(async () => {
      if (statusReading || running.current) return;
      statusReading = true;
      try {
        await refresh(
          projectRef.current?.state === 'collecting' ? ['status', 'preflight'] : ['status'],
        );
      } finally {
        statusReading = false;
      }
    }, 3000);
    const eventsTimer = setInterval(async () => {
      if (eventsReading || running.current) return;
      eventsReading = true;
      try {
        await refresh(searchRef.current?.active ? ['events', 'search'] : ['events']);
      } finally {
        eventsReading = false;
      }
    }, 5000);
    let previousLag = -1;
    const healthTimer = setInterval(async () => {
      if (healthReading) return;
      healthReading = true;
      try {
        const h = await api.get<Health>('/health');
        if (!mounted.current) return;
        setHealth(h);
        const cursor = liveStore.getSnapshot().sequence;
        if (h.last_event_id < cursor || (h.last_event_id > cursor && previousLag === cursor))
          void resync();
        previousLag = h.last_event_id > cursor ? cursor : -1;
      } catch {
        if (mounted.current) setHealth(undefined);
      } finally {
        healthReading = false;
      }
    }, 15000);
    let invalidationTimer: ReturnType<typeof setTimeout> | undefined;
    const invalidations = new Set<string>();
    liveStore.onInvalidate = (resources) => {
      for (const resource of resources) if (resource !== 'sessions') invalidations.add(resource);
      if (invalidationTimer) return;
      invalidationTimer = setTimeout(() => {
        invalidationTimer = undefined;
        void refresh([...invalidations]);
        invalidations.clear();
      }, 500);
    };
    liveStore.onGap = () => {
      clearTimeout(resyncTimer.current);
      resyncTimer.current = setTimeout(() => {
        void resync();
      }, 250);
    };
    return () => {
      clearInterval(statusTimer);
      clearInterval(eventsTimer);
      clearInterval(healthTimer);
      clearTimeout(invalidationTimer);
      liveStore.onGap = () => {};
      liveStore.onInvalidate = () => {};
    };
  }, [connected, api, liveStore, refresh, resync]);

  const run = useCallback(
    async <T>(
      label: string,
      operation: () => Promise<T>,
      resources: string[] = ['status', 'events', 'preflight'],
    ): Promise<T | undefined> => {
      if (running.current) {
        setAction({
          state: 'Error',
          text: 'An action is already executing; retry after its acknowledgement',
        });
        return;
      }
      running.current = true;
      setBusy(true);
      setAction({ state: 'Requested', text: label });
      try {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        setAction({ state: 'Executing', text: label + ' · waiting for core acknowledgement' });
        const result = await operation();
        if (mounted.current) {
          const queued =
            result !== null &&
            typeof result === 'object' &&
            'state' in result &&
            result.state === 'queued';
          setAction({
            state: queued ? 'Queued' : 'Confirmed',
            text:
              label +
              (queued ? ' · queued by core, awaiting execution' : ' · acknowledged by core'),
          });
          await refresh(resources);
        }
        return result;
      } catch (error) {
        if (mounted.current) setAction({ state: 'Error', text: label + ': ' + message(error) });
      } finally {
        running.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [refresh],
  );
  const command = (path: string, body: unknown = {}, resources?: string[]) =>
    run(path.replaceAll('/', ' '), () => api.project(projectId, path, body), resources);

  const connect = (node: string, token: string) =>
    run(
      'Connect node',
      async () => {
        await transport.connect(node, token);
        connectionEpoch.current++;
        setConnectionVersion((value) => value + 1);
        scope.current.dispose();
        currentScope.current = null;
        selection.current = '';
        setProjectId('');
        liveStore.stop();
        liveStore.scope('', '', '');
        setProjects([]);
        setSessions([]);
        setHealth(undefined);
        setCapabilities(undefined);
        setGuard(undefined);
        setPreflight(undefined);
        setEvents([]);
        setAllowed([]);
        setSearch(undefined);
        save('node', node);
        setConnected(true);
        if (connected) await boot();
      },
      [],
    );
  const nextProjects = () =>
    projectsCursor &&
    run(
      'Load projects',
      () => loadProjects(projectsCursor, [...projectsHistory, projectAfter.current]),
      [],
    );
  const previousProjects = () =>
    projectsHistory.length &&
    run(
      'Load previous projects',
      () => loadProjects(projectsHistory.at(-1), projectsHistory.slice(0, -1)),
      [],
    );
  return {
    api,
    live,
    project,
    projectId,
    setProjectId,
    projects,
    sessions,
    guard,
    preflight,
    events,
    allowed,
    search,
    action,
    busy,
    connected,
    connectionVersion,
    health,
    capabilities,
    refresh,
    run,
    command,
    connect,
    loadProjects,
    nextProjects,
    previousProjects,
    projectsCursor,
    projectsHistory,
    resync,
  };
}
export type Desk = ReturnType<typeof useDesk>;
