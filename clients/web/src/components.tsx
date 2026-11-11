import { t, getLocale } from './i18n';
import { normalizeUTC } from './wire';
import { useEffect, useRef, type ReactNode } from 'react';

export const hz = (value?: number | bigint | null) =>
  value == null
    ? '—'
    : value.toLocaleString(getLocale() === 'ru' ? 'ru-RU' : 'en-US') + ' ' + t('Hz');
export const utc = (value?: string) => {
  if (!value) return '—';
  try {
    return normalizeUTC(value).replace('T', ' ').replace('Z', ' UTC');
  } catch {
    return t('Invalid UTC time');
  }
};
export const titleCase = (value?: string) =>
  t(value ? value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase()) : 'Unknown');
export function Modal({
  label,
  close,
  wide,
  children,
}: {
  label: string;
  close: () => void;
  wide?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => {
      ref.current?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-label={t(label)}
      className={wide ? 'modal wide' : 'modal'}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      {children}
    </dialog>
  );
}
export function Panel({
  title,
  eyebrow,
  aside,
  children,
  className = '',
}: {
  title: string;
  eyebrow?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={'panel ' + className}>
      <div className="panel-heading">
        <div>
          {eyebrow && <div className="eyebrow">{t(eyebrow)}</div>}
          <h2>{t(title)}</h2>
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}
