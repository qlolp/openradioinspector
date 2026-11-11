import { useEffect, useRef, useState } from 'react';
import type { Comparison, DisplaySweep } from './types';

export function Spectrum({ sweep, comparison }: { sweep?: DisplaySweep; comparison?: Comparison }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current!;
    let acknowledgement = 0;
    let generation = 0;
    function draw() {
      const current = ++generation;
      cancelAnimationFrame(acknowledgement);
      delete canvas.dataset.sweepId;
      const rect = canvas.getBoundingClientRect(),
        scale = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, rect.width * scale);
      canvas.height = Math.max(1, rect.height * scale);
      const ctx = canvas.getContext('2d')!;
      ctx.scale(scale, scale);
      const w = rect.width,
        h = rect.height,
        left = 55,
        right = w - 16,
        top = 15,
        bottom = h - 32;
      const css = getComputedStyle(canvas),
        muted = css.getPropertyValue('--muted').trim(),
        grid = css.getPropertyValue('--line').trim();
      ctx.font = '11px ui-monospace, monospace';
      ctx.fillStyle = muted;
      ctx.strokeStyle = grid;
      ctx.lineWidth = 1;
      for (let db = -120; db <= 0; db += 20) {
        const y = bottom - ((db + 130) / 140) * (bottom - top);
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.fillText(String(db), 10, y + 4);
      }
      const hz = sweep?.frequencies_hz,
        low = hz?.[0] ?? 88000000,
        high = hz?.at(-1) ?? 108000000;
      const x = (value: number) => left + ((value - low) / (high - low || 1)) * (right - left);
      const y = (value: number) => bottom - ((value + 130) / 140) * (bottom - top);
      for (let i = 0; i <= 4; i++) {
        const frequency = low + ((high - low) * i) / 4,
          px = x(frequency);
        ctx.strokeStyle = grid;
        ctx.beginPath();
        ctx.moveTo(px, top);
        ctx.lineTo(px, bottom);
        ctx.stroke();
        ctx.fillStyle = muted;
        ctx.textAlign = i === 4 ? 'right' : i === 0 ? 'left' : 'center';
        ctx.fillText((frequency / 1e6).toFixed(3), px, h - 9);
      }
      const line = (frequencies: number[], levels: number[], color: string) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        for (let i = 0; i < levels.length; i++)
          i
            ? ctx.lineTo(x(frequencies[i]), y(levels[i]))
            : ctx.moveTo(x(frequencies[i]), y(levels[i]));
        ctx.stroke();
      };
      if (comparison?.compatible) {
        const frequencies = comparison.items.map((p) => p.frequency_hz);
        line(
          frequencies,
          comparison.items.map((p) => p.baseline_p95_dbm),
          '#ce8a15',
        );
        line(
          frequencies,
          comparison.items.map((p) => p.baseline_p50_dbm),
          '#6f8de6',
        );
      }
      if (sweep) {
        line(sweep.frequencies_hz, sweep.levels, css.getPropertyValue('--signal').trim());
        ctx.fillStyle = css.getPropertyValue('--signal').trim();
        const px = x(sweep.peak.frequency_hz),
          py = y(sweep.peak.level);
        ctx.beginPath();
        ctx.arc(px, py, 3, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.fillStyle = muted;
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for committed spectrum data', w / 2, h / 2);
      }
      if (sweep && rect.width > 0 && rect.height > 0) {
        // Two animation frames provide a paint opportunity after this draw.
        // Superseded draws cannot acknowledge a later source by accident.
        acknowledgement = requestAnimationFrame(() => {
          acknowledgement = requestAnimationFrame(() => {
            if (generation === current) canvas.dataset.sweepId = sweep.id;
          });
        });
      }
    }
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    draw();
    const theme = new MutationObserver(draw);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => {
      generation++;
      cancelAnimationFrame(acknowledgement);
      delete canvas.dataset.sweepId;
      observer.disconnect();
      theme.disconnect();
    };
  }, [sweep, comparison]);
  return (
    <canvas
      ref={ref}
      className="spectrum"
      role="img"
      aria-label={
        sweep
          ? `Spectrum: peak ${sweep.peak.level} ${sweep.level_unit} at ${sweep.peak.frequency_hz} Hz`
          : 'Spectrum waiting for measurements'
      }
    />
  );
}

// A fixed ring texture receives only new rows. The renderer never uploads the whole history per frame.
class WaterfallRenderer {
  private gl: WebGL2RenderingContext | null;
  private texture: WebGLTexture | null = null;
  private program: WebGLProgram | null = null;
  private vertexArray: WebGLVertexArrayObject | null = null;
  private row = 0;
  private count = 0;
  private last = '';
  private scratch = document.createElement('canvas');
  private fallback: CanvasRenderingContext2D;
  private height = 256;
  private width = 1024;
  constructor(
    private canvas: HTMLCanvasElement,
    fallbackCanvas: HTMLCanvasElement,
    private report: (mode: string) => void,
  ) {
    this.gl = canvas.getContext('webgl2', {
      alpha: false,
      antialias: false,
      preserveDrawingBuffer: true,
    });
    this.scratch.width = fallbackCanvas.width = this.width;
    this.scratch.height = fallbackCanvas.height = this.height;
    this.fallback = fallbackCanvas.getContext('2d')!;
    this.initialize();
  }
  initialize() {
    const gl = this.gl;
    if (!gl || gl.isContextLost()) {
      this.report('Canvas fallback');
      return;
    }
    const shader = (kind: number, source: string) => {
      const result = gl.createShader(kind)!;
      gl.shaderSource(result, source);
      gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS))
        throw Error('Waterfall shader unavailable');
      return result;
    };
    try {
      const vs = shader(
        gl.VERTEX_SHADER,
        '#version 300 es\nprecision highp float;out vec2 uv;void main(){vec2 p=vec2((gl_VertexID<<1)&2,gl_VertexID&2);uv=p;gl_Position=vec4(p*2.-1.,0.,1.);}',
      );
      const fs = shader(
        gl.FRAGMENT_SHADER,
        '#version 300 es\nprecision highp float;uniform sampler2D history;uniform int head;uniform int rows;in vec2 uv;out vec4 color;void main(){int age=int((1.-uv.y)*256.);if(age>=rows){color=vec4(.035,.055,.085,1.);return;}int y=(head-1-age+256)%256;color=texelFetch(history,ivec2(clamp(int(uv.x*1024.),0,1023),y),0);}',
      );
      this.program = gl.createProgram()!;
      gl.attachShader(this.program, vs);
      gl.attachShader(this.program, fs);
      gl.linkProgram(this.program);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS))
        throw Error('Waterfall program unavailable');
      this.texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8, this.width, this.height);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      this.vertexArray = gl.createVertexArray();
      this.row = 0;
      this.count = 0;
      this.last = '';
      this.report('WebGL2 · incremental rows');
    } catch {
      this.gl = null;
      this.report('Canvas fallback');
    }
  }
  lost() {
    this.report('Canvas fallback · graphics context lost');
  }
  restore() {
    this.initialize();
  }
  update(rows: DisplaySweep[]) {
    const lastIndex = rows.findIndex((s) => s.id === this.last);
    if (lastIndex < 0) {
      this.row = this.count = 0;
      this.fallback.clearRect(0, 0, this.width, this.height);
    }
    const pending = rows.slice(lastIndex + 1);
    const gl = this.gl && !this.gl.isContextLost() ? this.gl : null;
    const scratch = this.scratch.getContext('2d')!;
    for (const sweep of pending) {
      const pixels = new Uint8Array(this.width * 4),
        low = sweep.frequencies_hz[0],
        high = sweep.frequencies_hz.at(-1)!;
      let point = 0;
      for (let x = 0; x < this.width; x++) {
        const hz = low + ((high - low) * x) / (this.width - 1);
        while (
          point + 1 < sweep.levels.length &&
          Math.abs(sweep.frequencies_hz[point + 1] - hz) <
            Math.abs(sweep.frequencies_hz[point] - hz)
        )
          point++;
        const heat = Math.max(0, Math.min(1, (sweep.levels[point] + 120) / 100));
        pixels[x * 4] = Math.min(255, Math.max(0, heat - 0.42) * 440);
        pixels[x * 4 + 1] = Math.min(255, heat * 390);
        pixels[x * 4 + 2] = Math.max(35, 110 + heat * 140 - Math.max(0, heat - 0.6) * 500);
        pixels[x * 4 + 3] = 255;
      }
      if (gl) {
        gl.bindTexture(gl.TEXTURE_2D, this.texture);
        gl.texSubImage2D(
          gl.TEXTURE_2D,
          0,
          0,
          this.row,
          this.width,
          1,
          gl.RGBA,
          gl.UNSIGNED_BYTE,
          pixels,
        );
      }
      scratch.clearRect(0, 0, this.width, this.height);
      scratch.drawImage(this.fallback.canvas, 0, 1);
      this.fallback.clearRect(0, 0, this.width, this.height);
      this.fallback.drawImage(this.scratch, 0, 0);
      this.fallback.putImageData(
        new ImageData(new Uint8ClampedArray(pixels.buffer), this.width, 1),
        0,
        0,
      );
      this.row = (this.row + 1) % this.height;
      this.count = Math.min(this.height, this.count + 1);
      this.last = sweep.id;
    }
    if (gl && this.program) {
      this.canvas.width = this.width;
      this.canvas.height = this.height;
      gl.viewport(0, 0, this.width, this.height);
      gl.useProgram(this.program);
      gl.bindVertexArray(this.vertexArray);
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.uniform1i(gl.getUniformLocation(this.program, 'history'), 0);
      gl.uniform1i(gl.getUniformLocation(this.program, 'head'), this.row);
      gl.uniform1i(gl.getUniformLocation(this.program, 'rows'), this.count);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
  }
  dispose() {
    this.gl?.deleteTexture(this.texture);
    this.gl?.deleteProgram(this.program);
    this.gl?.deleteVertexArray(this.vertexArray);
  }
}

export function Waterfall({ rows }: { rows: DisplaySweep[] }) {
  const canvas = useRef<HTMLCanvasElement>(null),
    fallback = useRef<HTMLCanvasElement>(null),
    renderer = useRef<WaterfallRenderer | null>(null);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const [mode, setMode] = useState('Starting renderer');
  useEffect(() => {
    const element = canvas.current!;
    renderer.current = new WaterfallRenderer(element, fallback.current!, setMode);
    const lost = (event: Event) => {
      event.preventDefault();
      renderer.current?.lost();
    };
    const restored = () => {
      renderer.current?.restore();
      renderer.current?.update(rowsRef.current);
    };
    element.addEventListener('webglcontextlost', lost);
    element.addEventListener('webglcontextrestored', restored);
    renderer.current.update(rowsRef.current);
    return () => {
      element.removeEventListener('webglcontextlost', lost);
      element.removeEventListener('webglcontextrestored', restored);
      renderer.current?.dispose();
    };
  }, []);
  useEffect(() => {
    renderer.current?.update(rows);
  }, [rows]);
  return (
    <div className="waterfall-wrap">
      <div
        className="waterfall"
        role="img"
        aria-label={`Waterfall, ${rows.length} committed rows, newest at top`}
      >
        <canvas ref={canvas} hidden={!mode.startsWith('WebGL2')} />
        <canvas ref={fallback} hidden={mode.startsWith('WebGL2')} />
      </div>
      <div className="chart-caption">
        <span>UTC ↓ · newest first · display buffer {rows.length}/256</span>
        <span data-testid="renderer-state">{mode}</span>
      </div>
    </div>
  );
}
