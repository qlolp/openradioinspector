import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

test('browser pairing uses an HttpOnly secure cookie for API and EventSource, then revokes it', async ({
  page,
  context,
}) => {
  const origin = 'https://pairing.synthetic.invalid';
  let sawCookieOnEvents = false;
  await context.route(origin + '/**', async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    const headers = request.headers();
    const envelope = (data: unknown) =>
      JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data });
    if (url.pathname === '/api/v1/security/status')
      return route.fulfill({
        contentType: 'application/json',
        body: envelope({
          mode: 'lan_tls',
          pairing_required: !headers.cookie?.includes('ori_session=synthetic-only'),
        }),
      });
    if (url.pathname === '/api/v1/security/pair') {
      const body = request.postDataJSON();
      expect(body.client).toBe('browser');
      expect(headers.origin).toBe(origin);
      if (body.pairing_code !== 'synthetic-operator-code')
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            contract: 'openradioinspector-platform',
            contract_version: 1,
            error: { code: 'pairing_rejected', message: 'Pairing code rejected' },
          }),
        });
      return route.fulfill({
        contentType: 'application/json',
        headers: {
          'Set-Cookie': 'ori_session=synthetic-only; Secure; HttpOnly; SameSite=Strict; Path=/api',
        },
        body: envelope({ paired: true, credential: 'browser_cookie' }),
      });
    }
    if (url.pathname === '/api/v1/security/credentials/current/revoke') {
      expect(headers.origin).toBe(origin);
      expect(headers.cookie).toContain('ori_session=synthetic-only');
      return route.fulfill({
        contentType: 'application/json',
        headers: {
          'Set-Cookie': 'ori_session=; Secure; HttpOnly; SameSite=Strict; Path=/api; Max-Age=0',
        },
        body: envelope({ revoked: true }),
      });
    }
    if (url.pathname === '/api/v1/events') {
      sawCookieOnEvents = !!headers.cookie?.includes('ori_session=synthetic-only');
      return route.fulfill({
        contentType: 'text/event-stream',
        body: ': paired synthetic fixture\n\n',
      });
    }
    const response = await route.fetch({
      url: 'http://127.0.0.1:18772' + url.pathname + url.search,
    });
    return route.fulfill({ response });
  });
  await page.goto(origin + '/modern/');
  await expect(page.getByRole('heading', { name: 'Pair this browser' })).toBeVisible();
  await page.getByLabel('Operator pairing code').fill('wrong');
  await page.getByRole('button', { name: 'Pair browser', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Pairing code rejected');
  await page.getByLabel('Operator pairing code').fill('synthetic-operator-code');
  await page.getByRole('button', { name: 'Pair browser', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Spectrum desk' })).toBeVisible();
  await expect.poll(() => sawCookieOnEvents).toBe(true);
  const credential = (await context.cookies(origin + '/api/v1/security/status')).find(
    (cookie) => cookie.name === 'ori_session',
  );
  expect(credential).toMatchObject({
    httpOnly: true,
    secure: true,
    sameSite: 'Strict',
    path: '/api',
  });
  expect(await page.evaluate(() => document.cookie)).not.toContain('synthetic-only');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain(
    'synthetic-only',
  );
  await page.getByRole('button', { name: 'Connection settings', exact: true }).click();
  await page.getByRole('button', { name: 'Revoke this client', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Pair this browser' })).toBeVisible();
  expect(
    (await context.cookies(origin + '/api/v1/security/status')).some(
      (c) => c.name === 'ori_session',
    ),
  ).toBe(false);
});

test('a TLS legacy-browser entry pairs with its own cookie before opening the original interface', async ({
  page,
  context,
}) => {
  const origin = 'https://legacy-pairing.synthetic.invalid';
  let legacyApiAuthenticated = false;
  await context.route(origin + '/**', async (route) => {
    const url = new URL(route.request().url()),
      headers = route.request().headers();
    const envelope = (data: unknown) =>
      JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data });
    if (url.pathname === '/api/v1/security/status')
      return route.fulfill({
        contentType: 'application/json',
        body: envelope({
          mode: 'lan_tls',
          pairing_required: !headers.cookie?.includes('ori_session=synthetic-legacy-only'),
        }),
      });
    if (url.pathname === '/api/v1/security/pair') {
      expect(route.request().postDataJSON()).toMatchObject({
        client: 'browser',
        pairing_code: 'owned-legacy-code',
      });
      return route.fulfill({
        contentType: 'application/json',
        headers: {
          'Set-Cookie':
            'ori_session=synthetic-legacy-only; Secure; HttpOnly; SameSite=Strict; Path=/api',
        },
        body: envelope({ paired: true, credential: 'browser_cookie' }),
      });
    }
    if (url.pathname === '/')
      return route.fulfill({
        contentType: 'text/html',
        body: '<!DOCTYPE html><h1>Legacy interface fixture</h1><script>fetch("/api/v1/health")</script>',
      });
    if (url.pathname === '/api/v1/health') {
      legacyApiAuthenticated = !!headers.cookie?.includes('ori_session=synthetic-legacy-only');
      return route.fulfill({
        contentType: 'application/json',
        body: envelope({ status: 'ok', last_event_id: 0, sessions: 0 }),
      });
    }
    if (url.pathname === '/api/v1/events')
      return route.fulfill({ contentType: 'text/event-stream', body: ': synthetic fixture\n\n' });
    return route.fulfill({
      response: await route.fetch({ url: 'http://127.0.0.1:18772' + url.pathname + url.search }),
    });
  });
  await page.goto(origin + '/modern/?open=legacy');
  await expect(page.getByRole('heading', { name: 'Pair this browser' })).toBeVisible();
  await page.getByLabel('Operator pairing code').fill('owned-legacy-code');
  await page.getByRole('button', { name: 'Pair browser', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Legacy interface fixture' })).toBeVisible();
  await expect.poll(() => legacyApiAuthenticated).toBe(true);
  expect(page.url()).toBe(origin + '/');
  expect(page.url()).not.toContain('owned-legacy-code');
  expect(await page.evaluate(() => document.cookie)).not.toContain('synthetic-legacy-only');
});
