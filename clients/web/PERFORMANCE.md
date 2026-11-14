# Measured browser baseline

This is an instrumented diagnostic, not target-hardware acceptance. The 30-second producer, all offered-source accounting, three private browser pages and original render observer remained unchanged. Page and worker CPU sampling was added through Playwright CDP outside the repository. The core owned a new temporary archive and no hardware workers.

Frozen client: `a49c5491bcb746f5e7439e3f671267b71aad4629`; backend scalar correction: `1ea7501` (parent `d5b8893`); harness: `f69aabb19c18cde35f1fe0b924972111809b7f80`. Python core 3.12.14 / SQLite 3.53.1, producer Python 3.15.1, Node 26.13.0, x86-64 container with four-CPU cgroup quota, three colocated Chromium pages using SwiftShader. Source maps and profiler overhead were enabled. The measured window includes the observer's normal drain.

| Measurement | Observed |
| --- | --- |
| Planned / acknowledged / verified sweeps | 300 / 300 / 300, each 4,096 points |
| Planned / acknowledged / verified observations | 300 / 300 / 300 |
| WAV segments verified | 3 / 3 |
| Source drops, rejects, archive integrity failures | 0 |
| Draw markers | **896 / 900**, no duplicate or observer-dropped markers |
| Offered-to-render conservative p95 | **898.87 ms**, failed 250 ms reference |
| Main-thread TaskDuration per page | 11.76–12.51 s over ~33 s |
| Main-thread layouts per page | 534–566; layout duration 0.56–0.63 s |
| Worker sampled non-idle time per worker | ~3 s over ~33 s, mostly strict lexical parsing |
| Sampled peak core RSS | 206 MB |
| Sampled peak browser + harness tree RSS | 1.92 GB; shared pages counted repeatedly |

No endless animation loop was observed. Canvas row copying, image uploads, geometry reads and layout occur among the main-thread hot entries. Static inspection found that the spectrum recreates its resize observer on each source update and that the waterfall maintains a full Canvas history even with an active WebGL renderer. These are candidates for a separate measured rendering change; CPU samples do not establish an exact cost attribution or a hardware-GPU result. The four missing draw markers and failed latency gate remain failures. True commit-to-render latency, Raspberry Pi performance, power-loss behavior and physical-device acceptance remain unmeasured.

The retained report, source provenance, archive audit and all six CPU profiles are in the local verification artifact `ori-client-cpu30-_5k0v_xk`. Future comparisons must retain the original producer/observer denominator and clearly distinguish the durable-replay backend change from rendering changes.

## Shared spectrum and waterfall rendering

The spectrum now uses WebGL2 with Canvas recovery after context loss. Its resize observer is mounted once; backing dimensions change only on an actual resize. Source, theme, resize and local view interactions request one animation-frame draw. There is no idle animation loop. Source acknowledgements retain the two-animation-frame paint barrier; `data-render-frame` exposes completed draw acknowledgements for diagnostics without product debug text.

Wheel zoom, horizontal pointer dragging, arrow keys, +/− and Home/reset change only the local frequency view. They never request hardware tuning or alter source arrays. Projection subtracts the local integer-Hz origin in f64 before conversion to GPU float32, preserving one-Hz detail at high valid source frequencies. Clipped traces keep boundary intersections and the first visible maximum within a bounded vertex budget. The original global source peak stays unchanged, with an explicit message when it falls outside the displayed frequency range.

The waterfall uploads only new texture rows while GL is active. It no longer copies or uploads a hidden Canvas history. Canvas recovery reconstructs one image from the newest 256 retained source rows; subsequent Canvas rows use one self-copy and one row upload. Frequency buckets retain narrow maxima that nearest-point sampling could miss. These are display operations; archived f64 values, source indices, IDs and units remain unchanged.

Verification: 34 unit tests and all 17 functional browser tests passed, including real-core replay, source/audio evidence, RU/EN/mobile views, local pan/zoom without API mutations, context loss/restoration and zero hidden Canvas updates under GL. A previous full functional run had one stalled narrow-search query followed by guard-state cascade failures; its fresh isolated repeat and the full repeat passed with the same assertions and deadlines. Teardown now releases the explicitly owned synthetic guard even when a UI assertion fails. This does not establish the original stall's cause.

Run the separate interactive probe with `npm run test:interaction`. It uses a real archived 4,096-point source and the normal display reduction, then offers pan/wheel changes for a complete five-second window. On the measured x86-64 host, Chromium 156 with SwiftShader acknowledged **289 of 289** offered changes and range transitions over **5,007.1 ms**, at **57.51 acknowledged draws/s**. All source IDs stayed intact and no API mutations occurred. The JSON includes every frame timestamp, source hashes, renderer identity and CPU quota. The earlier probe also passed (298/298, 59.45 draws/s); neither run changes the denominator or increases a deadline.

These results cover one page and a static archived source. They do **not** establish continuous 10-Hz acquisition with three clients, Raspberry Pi throughput, physical display/GPU frame rate, Windows driver performance or the end-to-end 250-ms target. A fresh standard benchmark with the integrated durable backend and this client bundle is still required; the failed baseline above remains a failure. The final probe and functional logs are retained as `ori-chart-interaction-final` and `ori-chart-full-second` verification artifacts.

Baseline overlays now have explicit original/P95/P50 labels in both languages. The P95 stroke follows the theme's warning color instead of the previous low-contrast amber on white. A palette regression verifies at least 3:1 non-text contrast for the source and both essential baseline strokes in light/dark themes; labels use the normal readable text color. This is a focused contrast check, not a claim of a complete accessibility certification.
