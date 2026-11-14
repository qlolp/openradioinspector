import { useEffect } from 'react';
import { setLocale, t, useLocale, type Locale } from './i18n';
export function LocaleControls() {
  const locale = useLocale();
  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = 'OpenRadioInspector · ' + t('Spectrum desk');
  }, [locale]);
  return (
    <label className="theme-select locale-select">
      <span className="sr-only">{t('Language')}</span>
      <select
        value={locale}
        onChange={(event) => setLocale(event.target.value as Locale)}
        data-testid="locale-select"
      >
        <option value="ru" lang="ru">
          Русский
        </option>
        <option value="en" lang="en">
          English
        </option>
      </select>
    </label>
  );
}
