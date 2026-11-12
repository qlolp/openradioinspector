import { describe, expect, it, vi } from 'vitest';
import { disarmOwnedSyntheticGuard, type GuardCleanupRequest } from '../e2e/guard-cleanup';

const base = 'http://127.0.0.1:18771/api/v1';

function fixture() {
  // The hosted Python API list deliberately contains exactly these seven keys.
  const summary = {
    id: 'owned-project',
    name: 'North laboratory',
    site: 'Synthetic acceptance fixture',
    state: 'armed',
    active_session_id: 'owned-session',
    baseline_id: 'owned-baseline',
    autostart: false,
  };
  const project = {
    ...summary,
    profile: {
      analyzer: { settings: { adapter: 'tinysa-sim' } },
      receiver: { settings: { adapter: 'ardv1-sim' } },
    },
  };
  const status = { active: true, project };
  const list = { items: [summary] };
  const response = (data: unknown) => ({ json: async () => ({ data }) });
  const get = vi.fn(async (url: string) => {
    if (url === base + '/monitoring-projects') return response(list);
    if (url === base + '/monitoring-projects/owned-project/status') return response(status);
    throw new Error('Unexpected fixture read: ' + url);
  });
  const post = vi.fn(async () => ({ ok: (): boolean => true, text: async () => 'stopped' }));
  const request = { get, post } as unknown as GuardCleanupRequest;
  return { summary, project, status, list, request, get, post };
}

describe('owned synthetic guard cleanup', () => {
  it('uses the full status project after the real compact list and disarms only that fixture', async () => {
    const own = fixture();
    expect(Object.hasOwn(own.summary, 'profile')).toBe(false);
    await disarmOwnedSyntheticGuard(own.request);
    expect(own.get.mock.calls).toEqual([
      [base + '/monitoring-projects'],
      [base + '/monitoring-projects/owned-project/status'],
    ]);
    expect(own.post.mock.calls).toEqual([
      [base + '/monitoring-projects/owned-project/disarm', { data: {} }],
    ]);
  });

  it('does not touch an inactive fixture', async () => {
    const own = fixture();
    own.status.active = false;
    await disarmOwnedSyntheticGuard(own.request);
    expect(own.post).not.toHaveBeenCalled();
  });

  it.each(['name', 'site'] as const)('does not touch a foreign list %s', async (field) => {
    const own = fixture();
    own.summary[field] = 'foreign';
    await disarmOwnedSyntheticGuard(own.request);
    expect(own.get).toHaveBeenCalledTimes(1);
    expect(own.post).not.toHaveBeenCalled();
  });

  it.each(['id', 'name', 'site'] as const)(
    'rejects a different status project %s before mutation',
    async (field) => {
      const own = fixture();
      own.project[field] = 'foreign';
      await expect(disarmOwnedSyntheticGuard(own.request)).rejects.toThrow();
      expect(own.post).not.toHaveBeenCalled();
    },
  );

  it.each(['analyzer', 'receiver'] as const)(
    'rejects a physical %s adapter before mutation',
    async (role) => {
      const own = fixture();
      own.project.profile[role].settings.adapter =
        role === 'analyzer' ? 'tinysa-serial' : 'ardv1-serial';
      await expect(disarmOwnedSyntheticGuard(own.request)).rejects.toThrow();
      expect(own.post).not.toHaveBeenCalled();
    },
  );

  it('retains a failed disarm as a test failure without retry', async () => {
    const own = fixture();
    own.post.mockResolvedValue({ ok: () => false, text: async () => 'owned disarm failed' });
    await expect(disarmOwnedSyntheticGuard(own.request)).rejects.toThrow('owned disarm failed');
    expect(own.post).toHaveBeenCalledTimes(1);
  });
});
