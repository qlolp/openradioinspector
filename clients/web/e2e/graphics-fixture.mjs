// TEST ONLY. This changes the policy's identity input, never the real GL driver,
// shader implementation, pixels, context loss or application configuration.
export function hardwareIdentityForRealGLTests() {
  const original = WebGL2RenderingContext.prototype.getParameter;
  const seen = new WeakSet();
  const actual = [];
  Object.defineProperty(window, '__oriTestHardwareIdentityFixture', {
    value: {
      label:
        'TEST ONLY hardware identity; underlying real GL renderer recorded separately; no hardware validation',
      actual,
    },
  });
  Object.defineProperty(window, '__oriActualRendererForHardwareIdentityTest', {
    value: (gl) => {
      const identity = gl.getExtension('WEBGL_debug_renderer_info');
      return original.call(gl, identity ? identity.UNMASKED_RENDERER_WEBGL : gl.RENDERER);
    },
  });
  WebGL2RenderingContext.prototype.getParameter = function (parameter) {
    const value = original.call(this, parameter);
    if (parameter !== 0x9246) return value;
    if (!seen.has(this) && actual.length < 32) {
      seen.add(this);
      actual.push({ renderer: typeof value === 'string' ? value.slice(0, 1024) : null });
    }
    return 'TEST ONLY classified hardware identity; actual renderer recorded separately';
  };
}
