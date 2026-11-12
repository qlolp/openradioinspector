// SwiftShader can create a context even with failIfMajorPerformanceCaveat.
// Unknown or privacy-restricted identities keep the existing WebGL path.
export function isSwiftShaderContext(gl: WebGL2RenderingContext): boolean {
  try {
    const identity = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = identity && gl.getParameter(identity.UNMASKED_RENDERER_WEBGL);
    return typeof renderer === 'string' && /\bSwiftShader\b/i.test(renderer);
  } catch {
    return false;
  }
}
