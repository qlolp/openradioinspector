export { Spectrum } from './spectrum';
import { t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import type { DisplaySweep } from './types';
import { waterfallPixels } from './spectrum-view';

// A fixed ring texture receives only new rows. The renderer never uploads the whole history per frame.
class WaterfallRenderer {
  private gl: WebGL2RenderingContext | null;
  private texture: WebGLTexture | null = null;
  private program: WebGLProgram | null = null;
  private vertexArray: WebGLVertexArrayObject | null = null;
  private row = 0;
  private count = 0;
  private last = '';
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
      preserveDrawingBuffer: false,
    });
    canvas.width = fallbackCanvas.width = this.width;
    canvas.height = fallbackCanvas.height = this.height;
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
  lost(rows: DisplaySweep[]) {
    this.report('Canvas fallback · graphics context lost');
    this.last = '';
    this.update(rows);
  }
  restore() {
    this.initialize();
  }
  update(rows: DisplaySweep[]) {
    if (rows.length > this.height) rows = rows.slice(-this.height);
    const lastIndex = rows.findIndex((s) => s.id === this.last);
    const reset = lastIndex < 0;
    if (reset) {
      this.row = this.count = 0;
    }
    const pending = rows.slice(lastIndex + 1);
    const gl = this.gl && !this.gl.isContextLost() ? this.gl : null;
    // Rebuild Canvas once from the bounded source rows after loss/scope change.
    // While GL is active there is no hidden Canvas draw/copy/upload work.
    const history = !gl && reset ? new Uint8ClampedArray(this.width * this.height * 4) : undefined;
    for (let index = 0; index < pending.length; index++) {
      const sweep = pending[index],
        pixels = waterfallPixels(sweep, this.width);
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
      } else if (history) history.set(pixels, (pending.length - 1 - index) * this.width * 4);
      else {
        this.fallback.drawImage(this.fallback.canvas, 0, 1);
        this.fallback.putImageData(
          new ImageData(new Uint8ClampedArray(pixels.buffer), this.width, 1),
          0,
          0,
        );
      }
      this.row = (this.row + 1) % this.height;
      this.count = Math.min(this.height, this.count + 1);
      this.last = sweep.id;
    }
    if (history) this.fallback.putImageData(new ImageData(history, this.width, this.height), 0, 0);
    if (gl && this.program) {
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
      renderer.current?.lost(rowsRef.current);
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
        aria-label={t('Waterfall, {count} committed rows, newest at top', { count: rows.length })}
      >
        <canvas ref={canvas} hidden={!mode.startsWith('WebGL2')} />
        <canvas ref={fallback} hidden={mode.startsWith('WebGL2')} />
      </div>
      <div className="chart-caption">
        <span>
          {t('UTC ↓ · newest first · display buffer') + ' '}
          {rows.length}/256
        </span>
        <span data-testid="renderer-state">{t(mode)}</span>
      </div>
    </div>
  );
}
