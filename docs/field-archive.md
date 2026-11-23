# Portable field archive V1

An `.orifield` file contains one closed acquisition session, its original measurements, and the field log. All text is UTF-8. The file opens offline. An archive can include operator notes and precise geographic coordinates.

## Container

Use classic ZIP with exactly four flat, unencrypted, STORED entries. Sizes and CRC-32 are in the local headers. Data descriptors, ZIP64, duplicate entries and archive comments are outside V1. The complete archive must fit in 4,294,967,294 bytes. Write the manifest last. Readers must validate the complete archive before publishing or importing data. SHA-256 and ZIP CRC check integrity. They provide no signature or provenance.

| Member | Contents | Manifest record count |
| --- | --- | --- |
| `measurements.ndjson` | Existing `openradioinspector-archive-ndjson` version 1 | Sweep and observation envelopes, excluding the header |
| `events.ndjson` | Headerless field events in capture order | All events |
| `track.geojson` | GeoJSON FeatureCollection of WGS84 Points in GPS event order | GPS events |
| `manifest.json` | Version, session, source inventory, counts and hashes | No self-hash |

The manifest object has `format: "openradioinspector-field"`, integer `format_version: 1`, `session_id`, `stream_ids` (1–256 unique nonempty strings), `streams` (one descriptor per stream with `id`, `device`, `profile` and `settings`), integer `sweep_count` and `observation_count`. `files` maps the three exact payload names to `{bytes, records, sha256}`. Counts are nonnegative integers. Hashes are lowercase hexadecimal over the exact uncompressed payload bytes, including all line terminators. Limit the manifest to 256 KiB, each NDJSON or GeoJSON feature line to 16 MiB + 1024 bytes including LF. Writers must fail before delivering an oversized archive. Readers can ignore added object properties within this version.

## Measurements and field log

The first measurement line identifies the archive NDJSON format, its version and session. Every later line has `kind` (`sweep` or `observation`) and the existing common `measurement` object. Preserve measurement ID, session and stream ID, acquisition interval, integer Hz, original units, levels and all source settings or metadata. A receiver S-meter count stays in its reported units. See [the common fixture](../contracts/fixtures/measurement-v1.json). Manifest stream settings capture the requested configuration. Source records retain the instrument's readback.

Every event has `kind`, a nonempty string `id`, `session_id`, `stream_id` and an RFC 3339 `time_utc` with a UTC offset. Optional `sweep_id` or `observation_id` references a measurement in the same session and stream. At most one may be present. IDs are opaque and scoped by session, stream and kind. Physical row numbers, timestamps and filenames are never IDs.

| Event kind | Required fields | Optional fields |
| --- | --- | --- |
| `gps` | Numeric `latitude` from −90 to 90, `longitude` from −180 to 180 | Nonnegative `accuracy_m`, finite `altitude_m`, provider details |
| `note` | Nonempty `text` | Measurement reference |
| `gap` | Nonempty `reason` | Measurement reference, timing details |

A sweep may reference its recorded GPS fix in `settings.field_fix_id`. An observation uses `metadata.field_fix_id`. The fix must exist in the same stream. Missing links mean no fix was assigned. Readers must not fabricate a fix or infer an association from proximity. Forward references are valid. In particular GPS acquisition may occur before the first measurement. An empty track is valid.

## Track and recovery

To support single-pass validation with bounded memory, write the literal first line `{"type":"FeatureCollection","features":[`, followed by LF. Each GPS event contributes one compact Feature on its own line. Prefix the second and later feature lines with a comma. Terminate every feature line with LF, and finish with `]}\n`. Each feature has a string `id` equal to the fix ID and Point geometry with coordinates `[longitude, latitude]` and optional third `altitude_m`. Its properties copy every event field except `kind`, `id`, `latitude`, `longitude` and `altitude_m`. GPS-free sessions still contain both framing lines. Individual points preserve gaps and do not imply uninterrupted travel between fixes.

Android exports from a closed, immutable private database, stages a complete package, then copies it to the chosen Storage Access Framework document. Providers control final write durability. After interruption select a new destination and export again from the private session. A reader rejects a partial copy.

### Reviewing levels along a route on Android

Open **Архив** and select a completed phone session, or tap **Открыть .orifield…** and choose an exported document in the Android file picker. Select the measurement stream if the file contains several, then choose **Трек GPS**, enter a frequency in MHz and tap **Применить**. A colored point uses the measurement’s recorded fix ID in the same session and stream. The color scale spans the lowest and highest measured levels on the displayed page. A hollow circle marks a fix without a compatible sample. A cross marks a fix whose linked measurements missed the selected frequency.

For RF Explorer and tinySA, the color uses the nearest recorded bin inside each sweep, in dBm. Ties select the lower frequency. AR-DV1 uses an exact frequency match in raw S-meter counts. When several measurements cover the frequency at one fix, the last matching record with a level in capture order supplies the color. A hollow marker can still open an observation whose count is missing. A cross opens the last linked off-frequency record. Tap a linked marker to see the original measurement and its actual frequency and level when available. Use **К треку** to return to the same point or **Измерения в точке** to browse its other records. Clear the frequency and tap **Применить** to return to the plain GPS track.

Android checks the ZIP boundaries, file inventory, CRCs, SHA-256 hashes, record counts, scoped references and GPS features before displaying a document. It reads the source file without modification and builds a private temporary index. **Закрыть файл** returns to saved phone sessions. Closing the document, cancelling verification or rejecting an invalid file removes its staging files. Opening a document after an app interruption also reclaims abandoned review files.

The Android viewer uses the V1 ZIP and line limits above. Allow storage for one private copy of the document and a SQLite index. The index stores SHA-256 keys for the opaque record IDs and links, the original byte ranges and their hashes, and a digest of each expected GPS feature. The importer checks UTF-8, JSON structure and duplicate names through a 32 KiB file window. The GPS check covers all copied properties, including unknown nested metadata. The comparison accepts different JSON escapes, object member order and spellings of the same decimal number.

When opening a review page, Android checks each selected record’s original bytes against its saved SHA-256. A page owns up to 1 MiB of records or one larger V1 record. Fields are decoded on demand. Text pages use character offsets and keep Unicode surrogate pairs together. Spectrum views page through 512 measured points at a time. A file can be opened again after an interrupted import or after freeing space. Individual JSON records support up to 100,000 values, 64 levels of nesting and numeric literals up to 128 characters.

The [synthetic example](../contracts/fixtures/field-archive-v1.orifield) contains a sweep, a receiver observation, GPS, a note and a gap. To verify an archive or extract its validated contents into a new directory, run:

```sh
python3 tools/field_archive.py verify scan.orifield
python3 tools/field_archive.py extract scan.orifield ./scan
```

The [Android export fixture](../contracts/fixtures/field-android-export-v1.orifield) was written by the Kotlin exporter on an Android 12 emulator in [run 45041301657](https://github.com/qlolp/openradioinspector/actions/runs/45041301657). Its two spectrum points and GPS fix are synthetic. Standalone Swift tests replay these bytes; Android CI also tests a fresh export.
