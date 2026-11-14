# Serial SCPI setters and responses

Serial SCPI sweep configuration commands (`STAR`, `STOP`, `RESB`, `POIN`) use
`SerialScpiInstrument.write`: they do not require a response or consume bytes.
Only query commands such as `*IDN?` and `TRAC:DATA? TRACE1` use the strict bounded
response reader. Empty/truncated, oversized, non-ASCII and malformed trace
responses remain errors, not empty spectra.

Regression tests use a serial transcript that is silent for setters and emits a
trace only after the query. They verify command order, absence of reads after
setters, the requested frequency grid and unchanged sample values. The shared
count-mismatch test also exercises the actual serial query rather than replacing
it with a stub. Run `python -m unittest tests.test_scpi_integrity`.

This is synthetic verification, not physical acceptance of an IFR/HP analyzer.
A device that emits unsolicited acknowledgements needs its own verified protocol
profile; this change does not silently discard extra response bytes.
