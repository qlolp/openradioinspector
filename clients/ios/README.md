# OpenRadioInspector Field for iPhone 0.1.0

Open a closed Android Field session from Files, or follow an existing session on a paired Mac or Raspberry Pi. The SwiftUI app targets iPhone on iOS 17 and later. It uses the shared Swift SDK and the version 1 field archive reader. Each selected source retains its integer frequencies, recorded levels, original units, acquisition interval and opaque identifier. Notes and GPS fixes open through the links recorded in the file.

## Offline review

Choose **Архив → Открыть из Файлов**, or tap an `.orifield` document in Files and choose ORI Field. The app keeps a read lease while the document is open. It verifies record counts, hashes, CRCs and cross-references before showing the session. Tap a row for its original record. From a spectrum use **Исходные значения** for the individual frequency and level pairs.

App Review can use **Архив → Открыть учебный выезд** immediately after launch, including in airplane mode. Tap **Заметка**, follow **Спектр**, then **Исходные значения**. Return to the spectrum and follow **GPS**. This exercise uses the repository's synthetic archive, requires no account, location permission, radio equipment or server, and includes a receiver observation in its reported S-meter units. [Field archive specification](../../docs/field-archive.md)

## Paired local reader

1. Use a Rust core on the same Wi-Fi network with LAN TLS enabled and a certificate for its actual DNS name or IP address. Follow the node installation instructions. The HTTPS origin includes the node's configured port. [Node TLS setup](../../docs/rust-core.md)
2. Transfer the public `.ori-runtime/security/public-ca.pem` to Files. On the node, display its SHA-256 fingerprint with `openssl x509 -in PATH/TO/public-ca.pem -noout -fingerprint -sha256`. Keep the CA private key on the node.
3. Issue `ori-core pair --data-dir NODE_DATA_DIR --scope reader` on the node. In **Узел**, enter the HTTPS origin, choose the public CA certificate, compare the fingerprint with the node, confirm it and enter the one-time code. Allow Local Network when iOS prompts. The reader checks the server's hostname, certificate chain, current validity and reader scope. Trust is local to this pairing.
4. Choose a session and an analyzer to see its most recent original spectrum. **История спектра** pages archived sweeps and opens each original. Choose a receiver to see its recent observations, reported frequency, mode and original S-meter unit. The event list shows the latest 64 committed entries after catching up from its cursor. An interrupted foreground connection resumes from the last validated page. Reopening the app starts from the 32 most recent node events. History remains pageable on the node.
5. **Отключиться** stops the foreground connection. **Удалить сопряжение** can revoke the current credential on the node or forget the local copy when offline. The credential, origin and CA are held together in the device-only Keychain. After a CA rotation, pair again with the replacement certificate.

## Build and distribution

Open `clients/ios/Field.xcodeproj` and choose scheme **Field**. CI uses Xcode 27.2 / Swift 6.4.2, an iOS simulator runtime and the repository's local Swift packages. From the repository root run the following in a fresh output directory.

```sh
xcrun swift test --package-path macos/SpektrKomandy
bash clients/ios/ci/simulator.sh "$(mktemp -d)"
```

For the TLS integration test, first build `cargo build --locked -p ori-core` and set `ORI_FIELD_CORE="$PWD/target/debug/ori-core"` for the script. CI supplies this binary and starts a disposable loopback replay core containing the checked-in synthetic measurement fixture. The iOS test rejects a different CA and hostname, pairs with a scoped code, inspects spectrum and receiver observations, resumes the committed event ledger and revokes the reader.

The script creates its own iPhone simulator, runs app and UI tests, and compiles the Release configuration for a generic iOS device. The simulator tests use synthetic fixtures. The test bundle also opens the recorded Android-emulator export. CI does not upload to Apple or require a distribution certificate.

The bundle identifier is `org.openradioinspector.field`, version `0.1.0`, initial build `1`. TestFlight and App Store distribution follow Android hardware acceptance of the field format. For that second wave, register the bundle ID in the operator's Apple Developer team, set the team in Xcode, and archive the **Release** scheme for iOS with a new build number for each upload. Validate the signed archive in Organizer. Record the TestFlight build, iPhone model and OS, offline Files handoff, source/notes/GPS parity, Local Network denial and re-enable, trustworthy certificate rejection, credential revocation and history catch-up on a paired real node before requesting review. Direct USB-C instrument support is outside this version's feature set.

The App Review notes should direct the reviewer to the offline synthetic exercise above. The app reads user-selected archives and communicates with the selected local node. It has no telemetry or location collection. Confirm the store privacy answers against the signed build. The support address is 9218907903@mail.ru. [Published support page](https://github.com/qlolp/openradioinspector/blob/612f8b7faa16ee3cf5dbbd2c324ffdd65fbef39c/docs/field/support.md) · [Published privacy page](https://github.com/qlolp/openradioinspector/blob/612f8b7faa16ee3cf5dbbd2c324ffdd65fbef39c/docs/field/privacy.md)

For App Store Connect, use the main-branch URLs after verifying both are publicly reachable at submission time. [Support URL](https://github.com/qlolp/openradioinspector/blob/main/docs/field/support.md) · [Privacy URL](https://github.com/qlolp/openradioinspector/blob/main/docs/field/privacy.md)
