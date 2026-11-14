import { expect, it } from 'vitest';
import { getLocale, setLocale, t } from '../src/i18n';
it('defaults to Russian and uses common operator terms', () => {
  expect(getLocale()).toBe('ru');
  expect(t('Monitoring')).toBe('Радиомониторинг');
  expect(t('Research')).toBe('Исследование');
});
it('switches language without altering values or translating operator labels', () => {
  setLocale('en');
  expect(t('Monitoring')).toBe('Monitoring');
  setLocale('ru');
  expect(t('History page {page}', { page: 3 })).toBe('Страница истории 3');
  expect(t('North laboratory')).toBe('North laboratory');
});

it('keeps both explicit catalogs complete for primary operator flows', async () => {
  const en = (await import('../src/locales/en.json')).default,
    ru = (await import('../src/locales/ru.json')).default;
  expect(Object.keys(en).sort()).toEqual(Object.keys(ru).sort());
  expect(Object.values(ru).every((value) => typeof value === 'string' && value.length > 0)).toBe(
    true,
  );
  for (const key of [
    'Arm guard',
    'Prepare a receive-only project',
    'Connection settings',
    'Manual ownership',
    'Load history',
    'Event details',
    'Run preflight',
    'Error',
  ])
    expect((ru as Record<string, string>)[key]).toMatch(/[А-Яа-яЁё]/);
});
