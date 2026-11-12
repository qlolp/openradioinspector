# Python UTC admission and exact checkout bytes

The private scientific RPC accepts the same strict UTC spelling on Python 3.10
and later: terminal `Z`, optional one-to-six-digit fractional seconds, valid
calendar values and a non-reversed interval. The existing regular expressions,
dimension checks and validation order remain authoritative. After those spelling
checks, one shared decoder pads a fractional part to six digits and uses a
numeric UTC offset only in its local `datetime.fromisoformat` argument. It never
changes the original request, stored timestamps, source DTOs or scientific math.
Offsets supplied in a request and fractions longer than six digits still fail.

`.gitattributes` fixes canonical source/generated text to LF on checkout,
including Windows checkouts with `core.autocrlf=true`. Fixture directories
(`fixtures` and the existing macOS `Fixtures` spelling) and binary archives are
explicitly opaque with text conversion disabled. No source tree conversion,
oracle hash normalization, fixture regeneration or measurement rewrite is part
of this change. The detector's frozen source SHA-256 remains
`140639faf39d26f730afc01831d9985937b2fd1f1ce249b1435235dea76e4020`.

The baseline reproduced Python 3.10 failures in both real frozen-oracle RPC
paths and the exact Windows CRLF detector hash
`9ca3424a44b9a60398fbc7e9c45d440985c8ad724710e3b7272095eced522e55`.
After the fix, all 40 focused tests passed on Python 3.10.22 and Python 3.12.14
without skips. They include all five Control and eight detection frozen results,
strict invalid timestamp/ordering cases, all seven accepted fractional spellings,
unchanged request bytes, and a real isolated Git checkout of canonical source,
generated text, every tracked raw fixture and binary archive. Controlled opaque
fixture probes also preserve meaningful CRLF and lone CR bytes.

The public `Contracts.py`/`Sweep` parser is unchanged. Its pre-existing Python
3.10 rejection of some short fractional spellings remains distinct from private
RPC admission; no claim of changed public parser behavior is made.

Run the focused checks with the desired supported interpreter:

```sh
python -m unittest -v tests.test_research_rpc_utc_portability tests.test_repository_checkout tests.test_research_rpc
```
