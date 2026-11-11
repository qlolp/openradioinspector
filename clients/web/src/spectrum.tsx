import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';
import { hz } from './components';
import { getLocale, t, useLocale } from './i18n';
import { SpectrumRenderer } from './spectrum-renderer';
import {
  clampRange,
  panRange,
  sourceLevels,
  sourceRange,
  zoomRange,
  type FrequencyRange,
} from './spectrum-view';
import type { Comparison, DisplaySweep } from './types';

export function Spectrum({ sweep, comparison }: { sweep?: DisplaySweep; comparison?: Comparison }) {
  const locale = useLocale();
  const plot = useRef<HTMLDivElement>(null),
    canvas = useRef<HTMLCanvasElement>(null),
    fallback = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<SpectrumRenderer | null>(null);
  const [view, setView] = useState<FrequencyRange | null>(null),
    [mode, setMode] = useState('Starting renderer');
  const full = sourceRange(sweep),
    range = clampRange(view ?? full, full),
    levels = sourceLevels(sweep);
  const current = useRef({ sweep, comparison, full, range });
  current.current = { sweep, comparison, full, range };
  const previousSource = useRef({
    session: sweep?.session_id,
    stream: sweep?.stream_id,
    low: full.low,
    high: full.high,
  });
  const dragging = useRef<
    { id: number; x: number; width: number; range: FrequencyRange } | undefined
  >(undefined);
  const change = useCallback((next: FrequencyRange) => {
    const data = current.current;
    const bounded = clampRange(next, data.full);
    current.current = { ...data, range: bounded };
    setView(bounded);
    renderer.current?.update(data.sweep, data.comparison, bounded);
  }, []);
  useEffect(() => {
    const element = plot.current!;
    const glCanvas = canvas.current!,
      fallbackCanvas = fallback.current!;
    const graph = new SpectrumRenderer(glCanvas, fallbackCanvas, setMode);
    renderer.current = graph;
    const resize = new ResizeObserver((entries) => {
      const size = entries[0]?.contentRect;
      if (size) graph.resize(Math.max(1, size.width - 60), Math.max(1, size.height - 42));
    });
    resize.observe(element);
    const lost = (event: Event) => {
      event.preventDefault();
      graph.lost();
    };
    const restored = () => graph.restore();
    const wheel = (event: WheelEvent) => {
      const data = current.current;
      if (!data.sweep) return;
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const anchor = (event.clientX - rect.left - 48) / Math.max(1, rect.width - 60);
      const delta =
        event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);
      change(
        zoomRange(
          data.range,
          data.full,
          Math.exp(Math.max(-2, Math.min(2, delta * 0.0015))),
          anchor,
        ),
      );
    };
    element.addEventListener('wheel', wheel, { passive: false });
    const theme = new MutationObserver(() => graph.theme());
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const system = matchMedia('(prefers-color-scheme: light)');
    const systemTheme = () => graph.theme();
    system.addEventListener('change', systemTheme);
    glCanvas.addEventListener('webglcontextlost', lost);
    glCanvas.addEventListener('webglcontextrestored', restored);
    graph.update(current.current.sweep, current.current.comparison, current.current.range);
    return () => {
      resize.disconnect();
      theme.disconnect();
      system.removeEventListener('change', systemTheme);
      element.removeEventListener('wheel', wheel);
      glCanvas.removeEventListener('webglcontextlost', lost);
      glCanvas.removeEventListener('webglcontextrestored', restored);
      graph.dispose();
      renderer.current = null;
    };
  }, []);
  useEffect(() => {
    const before = previousSource.current;
    const changed =
      before.session !== sweep?.session_id ||
      before.stream !== sweep?.stream_id ||
      before.low !== full.low ||
      before.high !== full.high;
    if (changed) {
      previousSource.current = {
        session: sweep?.session_id,
        stream: sweep?.stream_id,
        low: full.low,
        high: full.high,
      };
      current.current.range = full;
      setView(null);
      dragging.current = undefined;
      renderer.current?.update(sweep, comparison, full);
    } else renderer.current?.update(sweep, comparison, range);
  }, [sweep, comparison, view]);
  const pan = (direction: number) => {
    const data = current.current;
    change(panRange(data.range, data.full, (data.range.high - data.range.low) * direction * 0.2));
  };
  const zoom = (factor: number) => {
    const data = current.current;
    change(zoomRange(data.range, data.full, factor));
  };
  const reset = () => change(current.current.full);
  const key = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!current.current.sweep) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowLeft') pan(-1);
    else if (event.key === 'ArrowRight') pan(1);
    else if (event.key === '+' || event.key === '=' || event.key === 'ArrowUp') zoom(0.8);
    else if (event.key === '-' || event.key === 'ArrowDown') zoom(1.25);
    else if (event.key === 'Home' || event.code === 'KeyR') reset();
    else return;
    event.preventDefault();
  };
  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !current.current.sweep) return;
    const element = event.currentTarget;
    dragging.current = {
      id: event.pointerId,
      x: event.clientX,
      width: Math.max(1, element.getBoundingClientRect().width - 60),
      range: current.current.range,
    };
    element.setPointerCapture(event.pointerId);
    element.focus({ preventScroll: true });
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragging.current;
    if (!drag || drag.id !== event.pointerId) return;
    change(
      panRange(
        drag.range,
        current.current.full,
        (-(event.clientX - drag.x) / drag.width) * (drag.range.high - drag.range.low),
      ),
    );
  };
  const up = (event: PointerEvent<HTMLDivElement>) => {
    if (dragging.current?.id !== event.pointerId) return;
    dragging.current = undefined;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const outside =
    sweep && (sweep.peak.frequency_hz < range.low || sweep.peak.frequency_hz > range.high);
  const precision = Math.min(
    6,
    Math.max(3, Math.ceil(Math.log10(1e6 / (range.high - range.low))) + 1),
  );
  const axisFormat = useMemo(
    () =>
      new Intl.NumberFormat(locale === 'ru' ? 'ru-RU' : 'en-US', {
        minimumFractionDigits: 3,
        maximumFractionDigits: precision,
      }),
    [locale, precision],
  );
  const axis = (value: number) => axisFormat.format(value / 1e6);
  const label = sweep
    ? t('Spectrum: peak {level} {unit} at {frequency} Hz', {
        level: sweep.peak.level,
        unit: sweep.level_unit,
        frequency: sweep.peak.frequency_hz,
      })
    : t('Spectrum waiting for measurements');
  const levelLabel = (value: number) =>
    Math.abs(value) >= 1e7 || (value !== 0 && Math.abs(value) < 0.001)
      ? value.toExponential(0)
      : value.toLocaleString(getLocale(), { maximumFractionDigits: 1 });
  return (
    <div
      className="spectrum-chart"
      data-testid="spectrum-chart"
      data-view-low={range.low}
      data-view-high={range.high}
    >
      <div
        ref={plot}
        className="spectrum-plot"
        tabIndex={0}
        role="group"
        aria-label={t('Spectrum view controls')}
        aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown + - Home R"
        onKeyDown={key}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
      >
        <canvas
          ref={canvas}
          className={mode.startsWith('WebGL2') ? 'spectrum' : 'spectrum-layer'}
          hidden={!mode.startsWith('WebGL2')}
          role="img"
          aria-label={label}
        />
        <canvas
          ref={fallback}
          className={mode.startsWith('WebGL2') ? 'spectrum-layer' : 'spectrum'}
          hidden={mode.startsWith('WebGL2')}
          role="img"
          aria-label={label}
        />
        <span className="spectrum-unit" aria-hidden="true">
          {sweep?.level_unit}
        </span>
        <div className="spectrum-level-axis" aria-hidden="true">
          {Array.from({ length: 5 }, (_, i) => (
            <span key={i} style={{ top: `${i * 25}%` }}>
              {levelLabel((levels.high / 4) * (4 - i) + (levels.low / 4) * i)}
            </span>
          ))}
        </div>
        <div className="spectrum-frequency-axis" aria-hidden="true">
          {Array.from({ length: 5 }, (_, i) => (
            <span key={i} style={{ left: `${i * 25}%` }}>
              {axis(range.low + ((range.high - range.low) * i) / 4)}
            </span>
          ))}
        </div>
        {!sweep && (
          <span className="spectrum-empty">{t('Waiting for committed spectrum data')}</span>
        )}
      </div>
      <div className="spectrum-controls" role="group" aria-label={t('Display frequency range')}>
        <button
          type="button"
          onClick={() => pan(-1)}
          disabled={!sweep}
          aria-label={t('Pan spectrum left')}
        >
          ←
        </button>
        <button
          type="button"
          onClick={() => zoom(0.5)}
          disabled={!sweep}
          aria-label={t('Zoom spectrum in')}
        >
          +
        </button>
        <button
          type="button"
          onClick={() => zoom(2)}
          disabled={!sweep}
          aria-label={t('Zoom spectrum out')}
        >
          −
        </button>
        <button
          type="button"
          onClick={() => pan(1)}
          disabled={!sweep}
          aria-label={t('Pan spectrum right')}
        >
          →
        </button>
        <button type="button" onClick={reset} disabled={!sweep}>
          {t('Reset view')}
        </button>
        <span className="muted" data-testid="spectrum-renderer-state">
          {t(mode)}
        </span>
      </div>
      <p className="spectrum-range mono">
        {hz(range.low)} — {hz(range.high)}
      </p>
      {comparison?.compatible && (
        <div className="spectrum-legend" aria-label={t('Spectrum comparison legend')}>
          <span>
            <i className="legend-source" aria-hidden="true" />
            {t('Original sweep')}
          </span>
          <span>
            <i className="legend-p95" aria-hidden="true" />
            {t('Baseline P95')}
          </span>
          <span>
            <i className="legend-p50" aria-hidden="true" />
            {t('Baseline P50')}
          </span>
        </div>
      )}
      {outside && (
        <p className="spectrum-peak-warning" role="status">
          {t('Source peak is outside the displayed frequency range')} ·{' '}
          {hz(sweep.peak.frequency_hz)}
        </p>
      )}
      <p className="spectrum-help">
        {t('View only · drag to pan, wheel or +/− to zoom, arrows to pan, Home to reset')}
      </p>
    </div>
  );
}
