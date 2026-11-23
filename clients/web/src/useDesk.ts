import { encodePathSegment } from './wire';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { Api, ResourceScope } from './api';
import { rfExplorerAvailable, standaloneRfExplorer } from './rf-explorer';
import { LiveStore, type LiveCheckpoint } from './live';
import { HealthReceipts, type HealthCheckpoint } from './health';
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
  StorageReport,
  Sweep,
} from './types';
const checkpoints = new WeakMap<
  Connection,
  {
    id: symbol;
    projectId: string;
    readOnly: boolean;
    state: LiveCheckpoint;
    health?: HealthCheckpoint;
  }
>();

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
const readyAction = {
  state: 'Ready',
  text: 'Receive-only workspace · core owns devices and archive',
};

export function useDesk(transport: Connection, rfSessionId?: string, readOnly = false) {
  const restoredCheckpoint = useMemo(() => {
    const saved = checkpoints.get(transport);
    return saved?.readOnly === readOnly ? saved : undefined;
  }, [transport, readOnly]);
  const api = useMemo(() => new Api(transport), [transport]);
  const healthReceipts = useMemo(
    () => new HealthReceipts(restoredCheckpoint?.health),
    [api, restoredCheckpoint],
  );
  const healthOwner = useRef(healthReceipts);
  healthOwner.current = healthReceipts;
  const restored = useMemo(() => {
    const saved = restoredCheckpoint;
    const store = new LiveStore(transport);
    if (saved) store.restore(saved.state);
    return { store, projectId: saved?.projectId, checkpointId: saved?.id };
  }, [transport, restoredCheckpoint]);
  const liveStore = restored.store;
  const live = useSyncExternalStore(liveStore.subscribe, liveStore.getSnapshot);
  const [projects, setProjects] = useState<Project[]>([]),
    [sessions, setSessions] = useState<Session[]>([]);
  const [projectId, setProjectId] = useState(() => (readOnly ? '' : remember('project', '')));
  const [guard, setGuard] = useState<GuardStatus>(),
    [preflight, setPreflight] = useState<Preflight>();
  const [storage, setStorage] = useState<StorageReport>();
  const storageRead = useRef(0);
  const [events, setEvents] = useState<MonitoringEvent[]>([]),
    [allowed, setAllowed] = useState<Allowed[]>([]),
    [search, setSearch] = useState<SearchStatus>();
  const [health, setHealth] = useState<Health>(),
    [capabilities, setCapabilities] = useState<Capabilities>();
  const [action, setAction] = useState(readyAction);
  const [busy, setBusy] = useState(false),
    [connected, setConnected] = useState(!transport.native || transport.ready === true);
  const [projectsCursor, setProjectsCursor] = useState<string | null>(null);
  const [projectsHistory, setProjectsHistory] = useState<string[]>([]);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const connectionEpoch = useRef(0);
  const selection = useRef(projectId);
  selection.current = projectId;
  const scope = useRef(new ResourceScope()),
    currentScope = useRef<{ generation: number; signal: AbortSignal } | null>(null);
  const running = useRef<symbol | false>(false),
    mounted = useRef(true),
    projectAfter = useRef('');
  const project = guard?.project;
  const scopeProject = useRef(restored.projectId);
  const rfSessions = useMemo(() => sessions.filter(standaloneRfExplorer), [sessions]);
  const rfSession = rfSessions.find((s) => s.id === rfSessionId) ?? rfSessions.at(-1);
  const rfActive = rfSessionId !== undefined && rfExplorerAvailable(capabilities?.features);
  const rfMode = useRef(rfActive);
  rfMode.current = rfActive;
  const requestedRf = useRef(rfSessionId);
  requestedRf.current = rfSessionId;
  const sessionsRead = useRef(0);

  const reloadSessions = useCallback(async () => {
    const epoch = connectionEpoch.current;
    const receipt = ++sessionsRead.current;
    const list = await api.get<Session[]>('/sessions');
    if (mounted.current && connectionEpoch.current === epoch && receipt === sessionsRead.current)
      setSessions(list);
  }, [api]);

  const loadProjects = useCallback(
    async (after = '', history: string[] = [], pin?: Project) => {
      if (readOnly) return;
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
    [api, readOnly],
  );

  const refresh = useCallback(
    async (
      resources: string[] = ['status', 'events', 'preflight', 'allowed', 'search', 'storage'],
    ) => {
      const id = selection.current,
        ticket = currentScope.current;
      if (readOnly || rfMode.current || !id || !ticket || ticket.signal.aborted) return;
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
      if (resources.includes('storage')) {
        const receipt = ++storageRead.current;
        const publish = (value?: StorageReport) => {
          if (valid() && receipt === storageRead.current) setStorage(value);
        };
        calls.push(read<StorageReport>('storage', publish).catch(() => publish()));
      }
      if (resources.includes('preflight')) calls.push(read<Preflight>('preflight', setPreflight));
      if (resources.includes('allowed'))
        calls.push(read<Page<Allowed>>('allowed?limit=200', (page) => setAllowed(page.items)));
      if (resources.includes('search')) calls.push(read<SearchStatus>('search/status', setSearch));
      const results = await Promise.allSettled(calls);
      const failed = results.find((r) => r.status === 'rejected');
      if (valid() && failed?.status === 'rejected' && failed.reason?.name !== 'AbortError')
        setAction({ state: 'Error', text: message(failed.reason) });
    },
    [api, readOnly],
  );

  const resyncRunning = useRef(false),
    resyncRequested = useRef(false),
    resyncTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const seed = useCallback(
    async (p: Project, searchScope?: SearchStatus, signal?: AbortSignal) => {
      if (rfMode.current || selection.current !== p.id || !searchScope) return;
      const sid =
        searchScope.active && searchScope.session_id ? searchScope.session_id : p.active_session_id;
      const stream =
        searchScope.active && searchScope.stream_id ? searchScope.stream_id : p.analyzer_stream_id;
      liveStore.scope(sid, stream ?? '', p.receiver_stream_id ?? '', p.active_session_id);
      if (!liveStore.needsSeed()) return;
      const [sweeps, observations] = await Promise.all([
        stream
          ? api.get<Sweep[]>(
              `/sessions/${encodePathSegment(sid)}/sweeps?` +
                new URLSearchParams({ stream_id: stream, limit: '1' }),
              signal,
            )
          : [],
        p.receiver_stream_id
          ? api.get<Observation[]>(
              `/sessions/${encodePathSegment(p.active_session_id)}/observations?` +
                new URLSearchParams({ stream_id: p.receiver_stream_id, limit: '1' }),
              signal,
            )
          : [],
      ]);
      if (!signal?.aborted && !rfMode.current && selection.current === p.id)
        liveStore.seed(sweeps, observations);
    },
    [api, liveStore],
  );
  const projectRef = useRef(project),
    searchRef = useRef(search);
  projectRef.current = project;
  searchRef.current = search;
  const resync = useCallback(async () => {
    if (!mounted.current) return;
    if (resyncRunning.current) {
      resyncRequested.current = true;
      return;
    }
    resyncRunning.current = true;
    const epoch = connectionEpoch.current;
    let cursor = liveStore.lastApplied();
    const receipt = healthReceipts.capture(cursor);
    liveStore.stop();
    try {
      const received = await api.get<Health>('/health');
      if (
        !mounted.current ||
        connectionEpoch.current !== epoch ||
        healthOwner.current !== healthReceipts
      )
        return;
      const accepted = healthReceipts.accept(receipt, received);
      const value = accepted ? received : healthReceipts.value;
      if (!value) {
        resyncRequested.current = true;
        return;
      }
      if (accepted) setHealth(value);
      else cursor = liveStore.lastApplied();
      if (healthReceipts.archiveChanged) {
        cursor = value.last_event_id;
        liveStore.resetArchive(cursor);
        healthReceipts.acknowledgeArchive(value);
        setConnectionVersion((version) => version + 1);
        setAction({ state: 'Confirmed', text: 'Archive changed — display reset' });
        if (rfMode.current) {
          ++sessionsRead.current;
          setSessions([]);
        }
        const p = projectRef.current;
        if (p && !rfMode.current) {
          try {
            await seed(p, searchRef.current, currentScope.current?.signal);
          } catch (error) {
            if ((error as Error).name !== 'AbortError') throw error;
          }
        }
      }
      if (mounted.current && connectionEpoch.current === epoch) await liveStore.start(cursor);
      await refresh(['status', 'events']);
    } catch (error) {
      if (
        mounted.current &&
        connectionEpoch.current === epoch &&
        healthOwner.current === healthReceipts &&
        (error as Error).name !== 'AbortError'
      ) {
        if (!healthReceipts.current(receipt)) {
          resyncRequested.current = true;
          return;
        }
        setAction({ state: 'Error', text: 'Resynchronization failed: ' + message(error) });
        resyncTimer.current = setTimeout(() => {
          void resync();
        }, 5000);
      }
    } finally {
      resyncRunning.current = false;
      if (resyncRequested.current && mounted.current) {
        resyncRequested.current = false;
        clearTimeout(resyncTimer.current);
        resyncTimer.current = setTimeout(() => {
          void resync();
        }, 250);
      }
    }
  }, [api, liveStore, seed, refresh, healthReceipts]);

  const boot = useCallback(async () => {
    const epoch = connectionEpoch.current;
    const receipt = healthReceipts.capture(liveStore.lastApplied());
    try {
      const [h, c, s] = await Promise.all([
        api.get<Health>('/health'),
        api.get<Capabilities>('/capabilities'),
        api.get<Session[]>('/sessions'),
      ]);
      if (
        !mounted.current ||
        connectionEpoch.current !== epoch ||
        healthOwner.current !== healthReceipts
      )
        return;
      const accepted = healthReceipts.accept(receipt, h);
      const value = accepted ? h : healthReceipts.value;
      if (!value) return;
      if (accepted) setHealth(value);
      setCapabilities(c);
      transport.configureStream?.(c);
      setSessions(s);
      const resume = liveStore.checkpoint();
      let after = resume?.sequence ?? value.last_event_id;
      if (healthReceipts.archiveChanged) {
        after = value.last_event_id;
        liveStore.resetArchive(after);
        healthReceipts.acknowledgeArchive(value);
        setConnectionVersion((version) => version + 1);
        setAction({ state: 'Confirmed', text: 'Archive changed — display reset' });
      }
      const wantsRf = () => requestedRf.current !== undefined && rfExplorerAvailable(c.features);
      if (wantsRf()) {
        const candidates = s.filter(standaloneRfExplorer);
        const selected =
          candidates.find((item) => item.id === requestedRf.current) ?? candidates.at(-1);
        if (selected) liveStore.scope(selected.id, selected.streams[0].id, '');
        else liveStore.selectProject(false);
      }
      if (readOnly || c.features.monitoring_projects === false) {
        selection.current = '';
        setProjectId('');
        setProjects([]);
      } else {
        await loadProjects();
      }
      // Establish the current selected scope before replay, including a source
      // search/session transition made by another operator while disconnected.
      const id = selection.current;
      if (resume && id && !wantsRf()) {
        const [status, search] = await Promise.all([
          api.project<GuardStatus>(id, 'status'),
          api.project<SearchStatus>(id, 'search/status'),
        ]);
        if (!mounted.current || connectionEpoch.current !== epoch) return;
        if (selection.current === id && !rfMode.current) {
          setGuard(status);
          setSearch(search);
          const p = status.project;
          liveStore.scope(
            search.active && search.session_id ? search.session_id : p.active_session_id,
            search.active && search.stream_id ? search.stream_id : (p.analyzer_stream_id ?? ''),
            p.receiver_stream_id ?? '',
            p.active_session_id,
          );
        }
      }
      if (mounted.current && connectionEpoch.current === epoch) await liveStore.start(after);
    } catch (error) {
      if (connectionEpoch.current === epoch) throw error;
    }
  }, [api, liveStore, loadProjects, healthReceipts, readOnly]);

  useEffect(() => {
    mounted.current = true;
    // Consume only after mount commits. An interrupted React render must not
    // discard the last applied cursor or allocate an abandoned parsing worker.
    if (restored.checkpointId && checkpoints.get(transport)?.id === restored.checkpointId)
      checkpoints.delete(transport);
    return () => {
      mounted.current = false;
      scope.current.dispose();
      clearTimeout(resyncTimer.current);
      const checkpoint = liveStore.checkpoint();
      if (checkpoint)
        checkpoints.set(transport, {
          id: Symbol(),
          readOnly,
          projectId: selection.current,
          state: checkpoint,
          health: healthReceipts.checkpoint(),
        });
      liveStore.dispose();
    };
  }, [liveStore, transport, restored.checkpointId, healthReceipts, readOnly]);

  useEffect(() => {
    if (connected)
      void boot().catch((error) => {
        if (mounted.current) setAction({ state: 'Error', text: message(error) });
      });
  }, [connected, boot]);

  const rfId = rfSession?.id;
  const rfStream = rfSession?.streams[0].id;
  useLayoutEffect(() => {
    if (rfActive) {
      scopeProject.current = undefined;
      if (rfId && rfStream) liveStore.scope(rfId, rfStream, '');
      else liveStore.selectProject(false);
      return;
    }
    if (scopeProject.current === projectId) return;
    scopeProject.current = projectId;
    liveStore.selectProject(Boolean(projectId));
  }, [projectId, liveStore, rfActive, rfId, rfStream]);

  useEffect(() => {
    currentScope.current = scope.current.next();
    setGuard(undefined);
    setPreflight(undefined);
    setStorage(undefined);
    setEvents([]);
    setAllowed([]);
    setSearch(undefined);
    if (!readOnly) save('project', projectId);
    if (connected) void refresh();
  }, [projectId, connectionVersion, connected, refresh, liveStore, rfActive, readOnly]);

  useEffect(() => {
    if (!rfActive || !rfId || !rfStream || !liveStore.needsSeed()) return;
    const controller = new AbortController();
    void api
      .get<Sweep[]>(
        '/sessions/' +
          encodePathSegment(rfId) +
          '/sweeps?' +
          new URLSearchParams({ stream_id: rfStream, limit: '1' }),
        controller.signal,
      )
      .then((sweeps) => {
        if (!controller.signal.aborted) liveStore.seed(sweeps, []);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setAction({ state: 'Error', text: message(error) });
      });
    return () => controller.abort();
  }, [api, liveStore, rfActive, rfId, rfStream, connectionVersion]);

  useEffect(() => {
    if (rfActive)
      void reloadSessions().catch((error) => {
        if (mounted.current && rfMode.current) setAction({ state: 'Error', text: message(error) });
      });
  }, [rfActive, reloadSessions, connectionVersion]);

  useEffect(() => {
    if (!project || !search || project.id !== projectId) return;
    const controller = new AbortController();
    void seed(project, search, controller.signal).catch((error) => {
      if (!controller.signal.aborted) setAction({ state: 'Error', text: message(error) });
    });
    return () => controller.abort();
  }, [
    projectId,
    project?.id,
    project?.active_session_id,
    project?.analyzer_stream_id,
    project?.receiver_stream_id,
    search?.active,
    search?.session_id,
    search?.stream_id,
    connectionVersion,
    seed,
  ]);

  useEffect(() => {
    if (!connected) return;
    let statusReading = false,
      eventsReading = false,
      healthReading = false,
      storageReading = false;
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
    const storageTimer = setInterval(async () => {
      if (storageReading || running.current) return;
      storageReading = true;
      try {
        await refresh(['storage']);
      } finally {
        storageReading = false;
      }
    }, 30000);
    const eventsTimer = setInterval(async () => {
      if (eventsReading || running.current) return;
      eventsReading = true;
      try {
        await refresh(searchRef.current?.active === false ? ['events'] : ['events', 'search']);
      } finally {
        eventsReading = false;
      }
    }, 5000);
    let previousLag: { epoch: symbol; cursor: bigint } | undefined;
    const healthTimer = setInterval(async () => {
      if (healthReading) return;
      healthReading = true;
      const epoch = connectionEpoch.current;
      const receipt = healthReceipts.capture(liveStore.lastApplied());
      try {
        const h = await api.get<Health>('/health');
        if (
          !mounted.current ||
          connectionEpoch.current !== epoch ||
          healthOwner.current !== healthReceipts
        )
          return;
        if (!healthReceipts.accept(receipt, h)) return;
        setHealth(h);
        const cursor = BigInt(liveStore.lastApplied());
        const highWater = BigInt(h.last_event_id);
        const applying = liveStore.isApplying();
        if (
          healthReceipts.archiveChanged ||
          (highWater > cursor &&
            previousLag?.epoch === receipt.epoch &&
            previousLag.cursor === cursor &&
            receipt.applied === cursor &&
            !applying)
        )
          void resync();
        previousLag =
          highWater > cursor && !applying ? { epoch: receipt.epoch, cursor } : undefined;
      } catch {
        if (
          mounted.current &&
          connectionEpoch.current === epoch &&
          healthOwner.current === healthReceipts &&
          healthReceipts.current(receipt)
        )
          setHealth(undefined);
      } finally {
        healthReading = false;
      }
    }, 15000);
    let invalidationTimer: ReturnType<typeof setTimeout> | undefined;
    const invalidations = new Set<string>();
    liveStore.onInvalidate = (resources) => {
      for (const resource of resources)
        if (resource !== 'sessions' || rfMode.current) invalidations.add(resource);
      if (invalidationTimer) return;
      invalidationTimer = setTimeout(() => {
        invalidationTimer = undefined;
        if (invalidations.delete('sessions') && rfMode.current)
          void reloadSessions().catch((error) => {
            if (mounted.current && rfMode.current)
              setAction({ state: 'Error', text: message(error) });
          });
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
      clearInterval(storageTimer);
      clearInterval(healthTimer);
      clearTimeout(invalidationTimer);
      liveStore.onGap = () => {};
      liveStore.onInvalidate = () => {};
    };
  }, [connected, api, liveStore, refresh, resync, healthReceipts, reloadSessions]);

  const run = useCallback(
    async <T>(
      label: string,
      operation: () => Promise<T>,
      resources: string[] = ['status', 'events', 'preflight'],
      readSignal?: AbortSignal,
    ): Promise<T | undefined> => {
      if (readSignal?.aborted) return;
      if (running.current) {
        setAction({
          state: 'Error',
          text: 'An action is already executing; retry after its acknowledgement',
        });
        return;
      }
      const owner = Symbol(label);
      running.current = owner;
      const current = () => mounted.current && running.current === owner && !readSignal?.aborted;
      let published = { state: 'Requested', text: label };
      let cancelFrame: (() => void) | undefined;
      const publish = (value: typeof published) => {
        published = value;
        setAction(value);
      };
      // Only explicitly scoped reads may release UI ownership on abort. Core
      // mutations keep the default acknowledgement lifecycle, even after a
      // project/view change. A discarded read never releases a newer action.
      const abortRead = () => {
        cancelFrame?.();
        if (running.current !== owner) return;
        running.current = false;
        if (mounted.current) {
          setBusy(false);
          setAction((value) => (value === published ? readyAction : value));
        }
      };
      setBusy(true);
      publish(published);
      readSignal?.addEventListener('abort', abortRead, { once: true });
      try {
        await new Promise<void>((resolve) => {
          const frame = requestAnimationFrame(() => resolve());
          cancelFrame = () => {
            cancelAnimationFrame(frame);
            resolve();
          };
        });
        cancelFrame = undefined;
        if (readSignal && !current()) return;
        publish({ state: 'Executing', text: label + ' · waiting for core acknowledgement' });
        const result = await operation();
        if (current()) {
          const queued =
            result !== null &&
            typeof result === 'object' &&
            'state' in result &&
            result.state === 'queued';
          publish({
            state: queued ? 'Queued' : 'Confirmed',
            text:
              label +
              (queued ? ' · queued by core, awaiting execution' : ' · acknowledged by core'),
          });
          await refresh(resources);
        }
        return readSignal && !current() ? undefined : result;
      } catch (error) {
        if (current()) publish({ state: 'Error', text: label + ': ' + message(error) });
      } finally {
        readSignal?.removeEventListener('abort', abortRead);
        if (running.current === owner) {
          running.current = false;
          if (mounted.current) setBusy(false);
        }
      }
    },
    [refresh],
  );
  const command = (path: string, body: unknown = {}, resources?: string[]) =>
    run(path.replaceAll('/', ' '), () => api.project(projectId, path, body), resources);

  const connect = (node: string) =>
    run(
      'Connect node',
      async () => {
        await transport.connect(node);
        connectionEpoch.current++;
        healthReceipts.resetNode();
        setConnectionVersion((value) => value + 1);
        scope.current.dispose();
        currentScope.current = null;
        selection.current = '';
        setProjectId('');
        liveStore.resetArchive();
        liveStore.scope('', '', '');
        setProjects([]);
        setSessions([]);
        setHealth(undefined);
        setCapabilities(undefined);
        setGuard(undefined);
        setPreflight(undefined);
        setStorage(undefined);
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
    rfSessions,
    rfSession,
    rfActive,
    guard,
    preflight,
    storage,
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
    reloadSessions,
    nextProjects,
    previousProjects,
    projectsCursor,
    projectsHistory,
    resync,
  };
}
export type Desk = ReturnType<typeof useDesk>;
