# Лицензии поставки Android Field

В AAB каталог `base/assets/notices/`, а в однофайловом APK — `assets/notices/`. Файл `licenses.json` перечисляет компоненты, лицензии, исходные имена уведомлений и SHA-256 их содержимого. Тексты находятся в `text/<sha256>.txt`. Для стандартной библиотеки Rust сохранён полный HTML-документ `COPYRIGHT-library.html` из зафиксированного toolchain. Одинаковые тексты занимают одно место и доступны по каждой записи инвентаря.

Для версии с указанным в этом каталоге `inventory.sha256` сборщик учитывает 49 Cargo-компонентов, 58 Android AAR/JAR, лицензию самого OpenRadioInspector и стандартную библиотеку Rust. Он получает точный граф нормальных и сборочных зависимостей `ori-android-field` из `cargo tree --locked -p ori-android-field --target aarch64-linux-android --edges normal,build`. Для каждого компонента он копирует фактические лицензии из Cargo registry. Три опубликованных JNI crate не включают тексты из корня исходного workspace. Для них сохранены исходные тексты по конкретным git-ревизиям из `.cargo_vcs_info.json`. [Лицензии JNI](https://github.com/jni-rs/jni-rs/tree/5ae9458a4ec44c5318f37ddc7569c1d4ae8a69e7) · [Лицензии JNI sys](https://github.com/jni-rs/jni-sys/tree/64d77b7a5f119d7b55b4e2c169a4668067ff59e6)

Gradle передаёт пути к фактически разрешённым AAR/JAR из `releaseRuntimeClasspath`. Пары Maven-координат и SHA-256 сверяются с `runtime-sha256.json`. Начальный набор сверён с `BUNDLE-METADATA/com.android.tools.build.libraries/dependencies.pb` исходной сборки и скачанными файлами. Сборка читает уведомления в AAR, runtime JAR, `classes.jar` и вложенных `libs/*.jar`. Дополнительные тексты и их SHA-256 зафиксированы в `origins.json`. Среди них лицензии Kotlin stdlib для GWT, Guava, ThreeTen и Boost, а также исходные NOTICE kotlinx.coroutines и kotlinx.serialization.

AAR USB-драйвера версии 3.9.0 приходит из JitPack. У него отсутствует собственный файл лицензии внутри AAR. Здесь сохранены исходная MIT-лицензия Google и Майка Уэйкерли, атрибуции отдельных драйверов и Apache 2.0 для Android `HexDump.java`. [Исходники USB-драйвера 3.9.0](https://github.com/mik3y/usb-serial-for-android/tree/e1018ab31c118b7d2e15fc7c2e9c5b19f33eb1f1)

## Сборка и проверка без сети

На машине с сетевым доступом установите JDK 17, Gradle Wrapper, закреплённый Rust и Android target, SDK 36 и NDK r28c по инструкции клиента. Выполните обычную сборку, включая `:app:testReleaseUnitTest :app:bundleRelease`, чтобы заполнить кэши Gradle Wrapper, плагинов, Maven и Cargo. Перенесите эти кэши и инструменты на автономную машину той же ОС и архитектуры, если сборка выполняется на другом компьютере. В репозитории хранятся исходники и уведомления, для первой установки toolchain понадобятся уже скачанные инструменты.

```sh
cd clients/android
CARGO_NET_OFFLINE=true ./gradlew --offline --no-daemon -PfieldVersionCode="$CODE" \
  :app:testReleaseUnitTest :app:bundleRelease
```

Защищённый workflow после прогрева выполняет чистую сборку без сетевого разрешения зависимостей и подписывает полученный AAB. Проверка сопоставляет индекс и каждый текст с зафиксированным хешем, в том числе после подписи AAB загрузочным ключом. Этап проверки Play берёт зафиксированный SHA инвентаря из **исходного коммита AAB**, повторяет проверку для загруженного файла и требует тот же индекс и те же тексты в выданном Google универсальном APK. В публичном `android-field-release.json` сохранён `notice_inventory_sha256`. Проверить пакет из соответствующей ревизии репозитория можно командами из корня репозитория.

```sh
python3 -m tools.field.android_notices verify field-upload.aab
python3 -m tools.field.android_notices verify candidate.apk
```

## Обновление зависимостей

1. Проверьте версии, исходные лицензии и дополнительные уведомления изменённых библиотек. При замене Maven-зависимостей сверьте SHA-256 подлинных AAR/JAR с источником и обновите соответствующие записи `runtime-sha256.json`. При изменении внешних текстов обновите `origins.json`.
2. Запустите `:app:collectFieldNotices`. Карта разрешённых путей сохраняется в `app/build/tmp/collectFieldNotices/resolved.json`. Из корня можно подготовить новый каталог для проверки командой ниже. Сверьте список компонентов и тексты, затем обновите `clients/android/notices/inventory.sha256` проверенным результатом в том же PR. Новый инвентарь не подменяет закреплённое значение автоматически.
3. Соберите AAB, обработайте его bundletool и проверьте оба пакета. Изменённый индекс, отсутствующий текст, подмена байтов или лишний файл останавливают выпуск.

```sh
python3 -m tools.field.android_notices collect --offline --review \
  --resolved clients/android/app/build/tmp/collectFieldNotices/resolved.json --assets ./field-notice-review
```
