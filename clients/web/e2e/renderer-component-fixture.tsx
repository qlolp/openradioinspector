// Test-only Vite module. This is not imported by the packaged application.
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { Spectrum } from '../src/spectrum';
import { decimate } from '../src/pipeline';
import type { Sweep } from '../src/types';
import '../src/styles.css';

export function mountSpectrumFixture(element: HTMLElement, initial?: Sweep) {
  const root = createRoot(element);
  const show = (source?: Sweep) => {
    flushSync(() =>
      root.render(createElement(Spectrum, { sweep: source ? decimate(source) : undefined })),
    );
  };
  show(initial);
  return { show, close: () => flushSync(() => root.unmount()) };
}
