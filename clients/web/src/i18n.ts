import { useSyncExternalStore } from 'react';
import en from './locales/en.json';
import ru from './locales/ru.json';
export type Locale = 'ru' | 'en';
const key = 'ori-modern-locale';
function initial(): Locale {
  if (typeof window === 'undefined') return 'ru';
  try {
    return localStorage.getItem(key) === 'en' ? 'en' : 'ru';
  } catch {
    return 'ru';
  }
}
let current = initial();
const listeners = new Set<() => void>();
export const getLocale = () => current;
export function setLocale(value: Locale): void {
  if (value !== 'ru' && value !== 'en') throw Error('Unsupported locale');
  if (current === value) return;
  current = value;
  try {
    if (typeof window !== 'undefined') localStorage.setItem(key, value);
  } catch {
    /* Preferences are optional. */
  }
  for (const listener of listeners) listener();
}
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export const useLocale = () => useSyncExternalStore(subscribe, getLocale, () => 'ru' as Locale);
/** Explicit resources only. Unknown operator/server text passes through unchanged. */
export function t(message: string, values: Record<string, unknown> = {}): string {
  const resources: Record<string, string> = current === 'ru' ? ru : en;
  const translate = (text: string, depth = 0): string => {
    if (Object.hasOwn(resources, text)) return resources[text];
    if (depth < 8 && text.includes(' · '))
      return text
        .split(' · ')
        .map((part) => translate(part, depth + 1))
        .join(' · ');
    const colon = text.indexOf(': ');
    if (depth < 8 && colon > 0 && Object.hasOwn(resources, text.slice(0, colon)))
      return (
        translate(text.slice(0, colon), depth + 1) +
        ': ' +
        translate(text.slice(colon + 2), depth + 1)
      );
    return text;
  };
  return translate(message).replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (token, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : token,
  );
}
