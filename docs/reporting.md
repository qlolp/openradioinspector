# Протоколы измерений и DOCX

`docx_report.py` формирует протокол напрямую в WordprocessingML без Pandoc, LibreOffice, `python-docx` или других обязательных зависимостей. Поддерживаются заголовки, статическое содержание, абзацы, таблицы с фиксированной шириной, header, footer и номер текущей страницы. Файл записывается атомарным replace.

## CLI

```bash
# Markdown/HTML-текст по-прежнему доступен.
python3 -m radioinspector report \
  --database ./data/sample.pan --output protocol.md

# Нативный DOCX.
python3 -m radioinspector report \
  --database ./data/sample.pan --format docx --output protocol.docx
```

`--format` принимает `md` (по умолчанию) и `docx`. Для DOCX обязателен `--output`. Шаблон передаётся через `--template` и обрабатывается тем же `ProtocolTemplate.render`, поэтому подстановка `{{key}}`, `#Keyword#` и маркеров таблиц `#D1#`…`#3D#` совпадает с текстовым протоколом.

## Границы

Поддерживается текстовый шаблон, а не бинарный `.doc` исходного комплекса. Парсер преобразует `#`, `##` и `###` в стили Heading1/2/3, а последовательные строки вида `| a | b |` — в таблицу с равной шириной колонок. Содержание статическое; Word может обновить поля при открытии документа (`updateFields=true`). Сложная вёрстка, изображения, сноски, отслеживание изменений и оригинальные binary templates не реализованы.

Пакет содержит `document.xml`, `styles.xml`, `settings.xml`, `header1.xml`, `footer1.xml`, relationships и свойства документа. Он проходит проверку OOXML/XSD и семантических связей; визуальный предпросмотр Word/WPS этим не заменяется.
