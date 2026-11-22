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

Файл `app/build/outputs/bundle/release/app-release.aab` служит заготовкой для Play. После приёмки владелец может подписать отдельную копию ключом загрузки. Команда `jarsigner` запросит пароли интерактивно:

```sh
jarsigner -keystore /path/to/upload.jks -signedjar field-upload.aab \
  app/build/outputs/bundle/release/app-release.aab upload
jarsigner -verify -verbose -certs field-upload.aab
```

Замените путь к хранилищу и псевдоним `upload` своими значениями. Рабочий процесс Android Field собирает пакеты из ревизии PR. Он проверяет подпись APK, выравнивание для 16 КиБ, ABI библиотек, минимальную и целевую версии Android, разрешения и целостность AAB. Пакеты и файл SHA-256 доступны в артефактах прогона.
