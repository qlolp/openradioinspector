# Web rendering on software GL

The spectrum and waterfall use their existing Canvas renderer when the exposed
`WEBGL_debug_renderer_info` renderer contains the explicit `SwiftShader` token.
Other renderer names, unavailable identity information and hardware GL retain
the WebGL path. The identity is checked during initialization and context
restoration, not for each frame. The policy does not alter measurement arrays,
units, source IDs, Worker decoding or applied-sequence acknowledgements.

`failIfMajorPerformanceCaveat` is insufficient for this choice: an observed
Chromium SwiftShader context accepted that option. The policy therefore uses
the narrow observed identity rather than treating all ANGLE, Google or Mesa
contexts as software. A software-selected waterfall performs its normal Canvas
updates without hidden GL texture uploads.

The shader and context-loss browser tests use a clearly labeled **test-only
hardware identity** over the real underlying GL context. The fixture changes
only the identity input seen by the selection policy; shaders, pixels and
context loss/restoration still run on the actual driver. Test artifacts record
that actual renderer separately. A separate browser test runs without that
fixture and verifies automatic SwiftShader-to-Canvas selection for both charts.
These tests do not establish hardware performance or Raspberry Pi acceptance.

Focused verification uses `tests/renderer-policy.test.ts` and
`e2e/renderer-policy.spec.ts`; the existing spectrum interaction and actual Core
interop profiles continue to cover the GL and Canvas implementations. The
interactive GL probe also records the identity fixture explicitly.
