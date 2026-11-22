# OpenRadioInspector Field

Android-приложение для записи выезда с одним USB-приёмником. Сборка предназначена для ARM64, Android 12 (API 31) и новее. Настройка и аппаратная приёмка описаны в [протоколе выезда](../../docs/field/release.md).

## Сборка

Установите JDK 17, Android SDK Command-line Tools и Rust через rustup. Укажите путь к SDK через `ANDROID_HOME` или `sdk.dir` в локальном `local.properties`. Из каталога `clients/android` выполните:

```sh
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  "platforms;android-36" "build-tools;36.0.0" "ndk;28.2.13676358"
rustup target add aarch64-linux-android
./gradlew :app:testDebugUnitTest :app:assembleDebug :app:bundleRelease
```

В Windows используется `gradlew.bat`. Gradle запускает Cargo с версией Rust из `rust-toolchain.toml` репозитория и помещает библиотеку JNI в оба пакета. APK для локальной установки подписывается отладочным ключом текущей машины. На телефоне с включённой отладкой по USB выполните:

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Установка поверх предыдущей версии требует того же сертификата. Одноразовые CI-раннеры создают собственные отладочные ключи. Перед заменой сборки с другой подписью экспортируйте сохранённые выезды, поскольку удаление приложения стирает его частное хранилище.

Для инженерной проверки рабочий процесс **Android Field** собирает debug APK и неподписанный release AAB. Он прогоняет Android-тесты, проверяет ARM64 и 16 КиБ и читает экспорт с эмулятора на Mac. При репетиции релиза используются два временных тестовых сертификата.

Для Google Play рабочий процесс **Sign Android Field upload bundle** выпускает отдельный AAB с закреплённой подписью загрузки. Для автономной установки распространяется исходный универсальный APK, подписанный Google Play. Подготовка ключей, сохранение данных отладочной версии, проверка обоих направлений обновления и выпуск независимого тега описаны в [инструкции издателя](../../docs/field/distribution.md).

Для воспроизведения сборки без сети сохраните кэши Gradle и Cargo, Android SDK/NDK и закреплённый Rust. Сборка помещает лицензию проекта и уведомления фактических Rust и Android runtime-зависимостей внутрь AAB/APK. [Инвентарь, автономная сборка и проверка пакета](notices/README.md)
