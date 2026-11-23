import { useEffect, useRef } from 'react';
import { hz } from './components';
import { getLocale, t } from './i18n';
import type { RfBounds } from './rf-explorer';
import type { PassDetail, PassPoint } from './rf-pass';

/** Both limits are measured frequencies. Adjacent measured grid cells leave no gap. */
export function missingRanges(limits: RfBounds, coverage: RfBounds[], step: number): RfBounds[] {
  const gaps: RfBounds[] = [];
  let low = limits.start_hz;
  for (const measured of coverage) {
    if (measured.start_hz > low) gaps.push({ start_hz: low, stop_hz: measured.start_hz - step });
    low = measured.stop_hz + step;
  }
  if (low <= limits.stop_hz) gaps.push({ start_hz: low, stop_hz: limits.stop_hz });
  return gaps;
}
export function pointAt(hz: number, columns: (PassPoint | null)[], coverage: RfBounds[]) {
  const run = coverage.find((r) => r.start_hz <= hz && hz <= r.stop_hz);
  if (!run) return null;
  let best: PassPoint | null = null;
  for (const p of columns)
    if (
      p &&
      p.frequency_hz >= run.start_hz &&
      p.frequency_hz <= run.stop_hz &&
      (!best || Math.abs(p.frequency_hz - hz) < Math.abs(best.frequency_hz - hz))
    )
      best = p;
  return best;
}
export function strongest(columns: (PassPoint | null)[]) {
  return columns.reduce<PassPoint | null>(
    (best, p) => (p && (!best || p.level_dbm > best.level_dbm) ? p : best),
    null,
  );
}
export function RfPassPlot({
  data,
  held,
  selected,
  marker,
  choose,
}: {
  data: PassDetail;
  held: boolean;
  selected: PassPoint | null;
  marker?: number;
  choose: (point: PassPoint | null) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const s = data.summary,
    { start_hz: low, stop_hz: high } = s;
  const columns = held ? (data.hold_columns ?? []) : data.columns,
    coverage = held ? (data.hold_coverage ?? []) : data.coverage;
  const axis = Intl.NumberFormat(getLocale(), { maximumFractionDigits: 6 });
  const levels = [...data.columns, ...(data.hold_columns ?? [])].flatMap((p) =>
    p ? [p.level_dbm] : [],
  );
  const floor = Math.min(-120, ...levels.map((v) => Math.floor(v / 10) * 10)),
    ceil = Math.max(0, ...levels.map((v) => Math.ceil(v / 10) * 10));
  useEffect(() => {
    const el = canvas.current!;
    function paint() {
      const ctx = el.getContext('2d');
      if (!ctx) return;
      const box = el.getBoundingClientRect(),
        scale = Math.min(2, devicePixelRatio || 1);
      el.width = Math.max(1, Math.round(box.width * scale));
      el.height = Math.max(1, Math.round(box.height * scale));
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      const w = box.width,
        h = box.height;
      if (!w || !h) return;
      const css = getComputedStyle(el);
      const x = (f: number) => ((f - low) / (high - low)) * (w - 1),
        y = (v: number) => 1 + ((ceil - v) / (ceil - floor)) * (h - 2);
      ctx.strokeStyle = css.getPropertyValue('--line');
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 4; i++) {
        ctx.moveTo(0, ((h - 1) * i) / 4);
        ctx.lineTo(w, ((h - 1) * i) / 4);
      }
      for (let i = 0; i <= 4; i++) {
        ctx.moveTo(((w - 1) * i) / 4, 0);
        ctx.lineTo(((w - 1) * i) / 4, h);
      }
      ctx.stroke();
      function draw(
        points: (PassPoint | null)[],
        runs: RfBounds[],
        style: string,
        dashed: boolean,
      ) {
        if (!ctx) return;
        ctx.strokeStyle = style;
        ctx.fillStyle = style;
        ctx.lineWidth = 1.5;
        ctx.setLineDash(dashed ? [5, 3] : []);
        let run = 0,
          lastRun = -1;
        ctx.beginPath();
        for (const p of points) {
          if (!p) continue;
          while (run < runs.length && p.frequency_hz > runs[run].stop_hz) run++;
          if (run >= runs.length || p.frequency_hz < runs[run].start_hz) continue;
          const px = x(p.frequency_hz),
            py = y(p.level_dbm);
          if (lastRun === run) ctx.lineTo(px, py);
          else {
            ctx.moveTo(px, py);
            ctx.fillRect(px - 1, py - 1, 2, 2);
          }
          lastRun = run;
        }
        ctx.stroke();
      }
      draw(data.columns, data.coverage, css.getPropertyValue('--signal'), false);
      if (data.hold_columns && data.hold_coverage)
        draw(data.hold_columns, data.hold_coverage, css.getPropertyValue('--warn'), true);
      ctx.setLineDash([]);
      if (marker !== undefined && marker >= low && marker <= high) {
        ctx.strokeStyle = css.getPropertyValue('--accent');
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(x(marker), 0);
        ctx.lineTo(x(marker), h);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (selected) {
        ctx.strokeStyle = css.getPropertyValue('--text');
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x(selected.frequency_hz), y(selected.level_dbm), 4, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    const size = new ResizeObserver(paint);
    size.observe(el);
    const theme = new MutationObserver(paint);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const system = matchMedia('(prefers-color-scheme: light)');
    system.addEventListener('change', paint);
    paint();
    return () => {
      size.disconnect();
      theme.disconnect();
      system.removeEventListener('change', paint);
    };
  }, [data, low, high, ceil, floor, selected, marker]);
  return (
    <div className="rf-pass-chart">
      <div className="rf-level-axis" aria-hidden="true">
        {[ceil, (floor + ceil) / 2, floor].map((v) => (
          <span key={v}>
            {v}
            {v === ceil ? ' dBm' : ''}
          </span>
        ))}
      </div>
      <canvas
        ref={canvas}
        role="group"
        tabIndex={0}
        aria-label={t('Survey spectrum in dBm. Click a measured frequency or use the arrow keys.')}
        aria-keyshortcuts="ArrowLeft ArrowRight Home End"
        onClick={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const hz =
            low +
            Math.round((((e.clientX - box.left) / box.width) * (high - low)) / s.step_hz) *
              s.step_hz;
          choose(pointAt(hz, columns, coverage));
        }}
        onKeyDown={(e) => {
          const points = columns.filter((p): p is PassPoint => p !== null);
          if (!points.length) return;
          const index = selected
            ? points.findIndex((p) => p.frequency_hz === selected.frequency_hz)
            : -1;
          let target: number;
          if (e.key === 'Home') target = 0;
          else if (e.key === 'End') target = points.length - 1;
          else if (e.key === 'ArrowRight') target = Math.min(points.length - 1, index + 1);
          else if (e.key === 'ArrowLeft')
            target = index < 0 ? points.length - 1 : Math.max(0, index - 1);
          else return;
          e.preventDefault();
          choose(points[target]);
        }}
      />
      <div className="rf-frequency-axis" aria-hidden="true">
        <span>{axis.format(low / 1e6)}</span>
        <span>{t('MHz')}</span>
        <span>{axis.format(high / 1e6)}</span>
      </div>
      <span className="sr-only">
        {hz(low)} – {hz(high)} · dBm
      </span>
    </div>
  );
}
