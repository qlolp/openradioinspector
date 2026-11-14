# Offline Web assets in the Rust candidate

Build and package the shared React client before compiling a distributable core:

```sh
npm ci --prefix clients/web --ignore-scripts --no-audit --no-fund
npm --prefix clients/web run build
python tools/build_modern_web.py
cargo build -p ori-core --release --locked
```

The core embeds only known regular files. `/modern` and `/modern/` serve the
packaged index; `/modern/assets/*` serves its exact hashed JS, worker and CSS
bytes with their proper MIME types. No CDN or runtime filesystem is needed.
The existing `/`, `/platform` and `/instrument` interfaces remain available.
The packaged manifest itself and arbitrary filesystem paths are not public.

Each asset is limited to 1 MiB, the combined legacy/modern bundle to 16 MiB,
and modern files to 256 entries/eight directory levels. Linked, special or
unsafe-named modern entries fail the build. A source-only build without a
packaged modern directory keeps the old interfaces and returns an explicit 503
at the modern entry point. `assets::modern_available()` reports the compiled
capability; a partial packaged directory is rejected at compilation.

The HTTP regression verifies every packaged file byte-for-byte, modern entry
points, legacy fallback, traversal/private-file rejection and shadow shutdown
without creating an archive. It was run both with the real packaged client and
without that directory. Repackage after changing the client before rebuilding;
the packaging tool publishes fresh build inputs atomically.
