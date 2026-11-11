import { useEffect, useRef, type ReactNode } from 'react';

export const hz = (value?: number | null) =>
  value == null ? '—' : value.toLocaleString('en-US') + ' Hz';
export const utc = (value?: string) => (value ? value.replace('T', ' ').replace('Z', ' UTC') : '—');
export const titleCase = (value?: string) =>
  value ? value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase()) : 'Unknown';
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
      aria-label={label}
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
          {eyebrow && <div className="eyebrow">{eyebrow}</div>}
          <h2>{title}</h2>
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}
