# Иконка приложения macOS

Иконка изображает спектр с тремя пиками, две дуги радиоволн и янтарный маркер на тёмно-синем фоне. Текст отсутствует, чтобы знак оставался читаемым в Dock.

## Ресурсы и сборка

Ресурсы: `macos/AppAssets.xcassets/AppIcon.appiconset`. Основной файл 1024×1024: `icon_512x512@2x.png`. Каталог содержит все десять вариантов macOS: 16, 32, 128, 256, 512 pt при 1×/2×. PNG сохраняют прозрачность за пределами плитки.

Каталог включён в Resources приложения Xcode; Debug и Release используют `ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon`. Ресурсы принадлежат launcher target, а не Swift-библиотеке. `actool` формирует `AppIcon.icns` и записи `CFBundleIconName`/`CFBundleIconFile`. Существующий release-скрипт копирует собранный `.app` вместе с ними.

Создано встроенным инструментом `image_gen` (навык imagegen), без CLI/API-ключа. Размеры экспортированы системным `sips`; исходное изображение визуально не перерисовывалось. Для восстановления размеров можно использовать сохранённый файл 1024×1024. Генерация по промпту не гарантирует побитового повторения изображения.

## Промпт генерации

```text
Use case: logo-brand. Create one finished macOS application icon for OpenRadioInspector, a professional radio spectrum monitoring application. Square 1024x1024 PNG asset. A bold, simple luminous cyan spectrum trace with three distinctly shaped frequency peaks, merging into two concentric radio-wave arcs, a small warm amber signal point. Deep navy rounded-square tile with subtle polished depth, generous transparent margin outside the tile, crisp silhouette, readable at 32 pixels. Balanced centered composition, restrained professional scientific instrumentation aesthetic. No letters, numbers, text, watermark, mockup, desktop, or extra surrounding objects. Actual transparent background outside the icon tile. Save the image as a local file and return its path for integration into an Xcode asset catalog.
```

## Исправление диагностики

Xcode показывал шесть предупреждений о доступе к `@State azimuth/elevation`, изолированным `MainActor`, из вспомогательной функции проекции в `Canvas`. Теперь `body` снимает неизменяемые значения ракурса на главном акторе; отрисовка использует этот снимок. Формула проекции, жесты и измерения не изменены. После сборки Issue Navigator: 0 ошибок / предупреждений.

Проверены Xcode BuildProject, размеры/alpha десяти PNG, наличие AppIcon в собранном bundle, тесты приложения и библиотеки, Python-проверки регистрации исходников и WebUI. Это ограниченная проверка сборки и регрессий, не утверждение об отсутствии всех возможных ошибок проекта.

Аппаратная проверка 2026-11-14: после перезапуска изображение `NSApp.applicationIconImage` совпадает с новой иконкой; 3D отображает реальные измерения tinySA Ultra. Приложение оставлено в LIVE/2D, 88–108 МГц, RBW 10 кГц; новые свипы поступают без ошибок, генератор выключен. Поворот 3D синтетическим перетаскиванием отдельно не подтверждён.
