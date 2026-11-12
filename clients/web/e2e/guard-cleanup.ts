import assert from 'node:assert/strict';
import type { APIRequestContext } from '@playwright/test';

export type GuardCleanupRequest = Pick<APIRequestContext, 'get' | 'post'>;

export async function disarmOwnedSyntheticGuard(request: GuardCleanupRequest) {
  // Only the temporary loopback fixture, with both explicitly synthetic adapters.
  const base = 'http://127.0.0.1:18771/api/v1';
  const projects = (await (await request.get(base + '/monitoring-projects')).json()).data.items;
  const summary = projects.find(
    (item: any) => item.name === 'North laboratory' && item.site === 'Synthetic acceptance fixture',
  );
  if (!summary) return;
  const status = (
    await (await request.get(base + '/monitoring-projects/' + summary.id + '/status')).json()
  ).data;
  if (!status.active) return;
  // List rows are compact summaries; status owns the full project/profile.
  const project = status.project;
  assert.equal(project.id, summary.id);
  assert.equal(project.name, summary.name);
  assert.equal(project.site, summary.site);
  assert.equal(project.profile.analyzer.settings.adapter, 'tinysa-sim');
  assert.equal(project.profile.receiver.settings.adapter, 'ardv1-sim');
  const stopped = await request.post(base + '/monitoring-projects/' + project.id + '/disarm', {
    data: {},
  });
  assert.equal(stopped.ok(), true, await stopped.text());
}
