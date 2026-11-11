import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test('Russian is default across operator flows and English switching preserves project, inputs and guard', async ({
  page,
}) => {
  test.setTimeout(60000);
  await openDesk(page);
  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
  await expect(page.getByRole('button', { name: 'Радиомониторинг', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Исследование', exact: true })).toBeVisible();
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  await expect(page.getByRole('button', { name: 'Включить охрану', exact: true })).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Проверить готовность', exact: true }),
  ).toBeVisible();
  const project = await page.getByTestId('project-select').inputValue(),
    guard = await page.getByTestId('guard-state').textContent();
  await page.getByRole('button', { name: 'Исследование', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Загрузить историю', exact: true })).toBeVisible();
  await page.getByLabel('Частота настройки (Гц)').fill('101500000');
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') mutations.push(request.url());
  });
  await page.getByTestId('locale-select').selectOption('en');
  await expect(page.getByRole('button', { name: 'Research', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  expect(await page.getByTestId('project-select').inputValue()).toBe(project);
  await expect(page.getByLabel('Tune frequency (Hz)')).toHaveValue('101500000');
  expect(mutations).toEqual([]);
  await page.getByTestId('locale-select').selectOption('ru');
  await expect(page.getByTestId('guard-state')).toHaveText(guard!);
  await expect(page.getByLabel('Частота настройки (Гц)')).toHaveValue('101500000');
  await page.getByRole('button', { name: '＋ Подготовить проект', exact: true }).click();
  await expect(
    page.getByRole('dialog', { name: 'Подготовка проекта только для приёма' }),
  ).toBeVisible();
  await expect(page.getByLabel('Название проекта')).toBeVisible();
  await page.getByRole('button', { name: 'Отмена', exact: true }).click();
  await page.getByRole('button', { name: 'Радиомониторинг', exact: true }).click();
  await page
    .getByTestId('event-row')
    .first()
    .getByRole('button', { name: 'Подробности', exact: true })
    .click();
  await expect(page.getByRole('dialog', { name: 'Подробности события' })).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Воспроизвести аудио', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Исходный спектр', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть событие', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await page.screenshot({ path: 'test-results/monitoring-ru-mobile.png', fullPage: true });
  const overflow = await page.evaluate(() =>
    Array.from(document.querySelectorAll('body *'))
      .filter((element) => {
        const box = element.getBoundingClientRect();
        return box.width > 0 && (box.right > innerWidth + 1 || box.left < -1);
      })
      .map((element) => ({
        tag: element.tagName,
        class: element.className,
        text: element.textContent?.slice(0, 90),
        width: element.getBoundingClientRect().width,
      }))
      .slice(0, 12),
  );
  expect(overflow).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
