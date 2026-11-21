# Обязательные проверки main

Проект политики задаёт **27 обязательных проверок GitHub Actions**. 21 ноября 2026 года GitHub App получил
HTTP 403 `Resource not accessible by integration` при чтении административной конфигурации. Включение
защиты пока не подтверждено.

После включения каждое изменение `main` проходит через PR с актуальной базой и закрытыми обсуждениями.
Правило распространяется на администраторов. Обязательных одобрений — ноль. Force-push и удаление
ветки запрещены. Полные имена и идентификатор источника (`app_id: 15368`, GitHub Actions) перечислены
в [main-protection.json](main-protection.json).

| Проверки | Сколько | Что проходит на каждом PR |
| --- | ---: | --- |
| `synthetic-*` | 12 | Ubuntu, Windows и macOS, на каждой ОС Python 3.10, 3.12, 3.14 и 3.15 |
| `modern-*` | 4 | Ubuntu x64, Windows, macOS и Ubuntu ARM64 |
| `Panorama storage on …` | 2 | Хранилище на Ubuntu ARM64 и Windows |
| `native-arm64` | 1 | Нативная сборка Raspberry Pi |
| Сборки пакетов | 6 | `python`, обе `desktop`, `native-review-regressions`, `macos-personal-rc`, `macos-audio-candidate` |
| `PR / Modern clients` | 1 | Для затронутых путей должны пройти Web, native bridge и Windows installer |
| `PR / Native reference load` | 1 | Для затронутых путей нагрузка должна пройти на Windows x64 и Linux ARM64 |

Две последние проверки публикует PR-workflow. Он сравнивает полные деревья Git от общей базы до точного HEAD из события PR. Переименования учитываются по старому и новому путям. Если в PR меняется только документация релиза, обе проверки явно подтверждают отсутствие затронутых путей. Изменение установленного руководства в `docs/operator-guide/` требует проверки клиентов. Общие входы сборки, включая `Cargo.toml`, `tools/build_modern_web.py`, исходники Web и встроенные ресурсы ядра, запускают обе проверки. Изменение кода владения процессами нагрузочного стенда запускает нативную нагрузку. Ошибка сравнения, неполная история, отмена или пропуск нужного задания блокируют соответствующую проверку. Фильтры и проверяемые результаты заданы в [pr_checks.py](../../tools/ci/pr_checks.py).

Для изменений нагрузочного стенда обычный режим занимает 60 секунд. Метка `full-reference-load` включает 1 800 секунд на обоих хостах и действует на последующие коммиты, пока она установлена. Её снятие пересчитывает проверку в обычном режиме. Посторонние метки и правка заголовка PR сохраняют текущий результат. Ручной запуск `windows-load.yml` принимает параметр `full`. У обычного push и ручного запуска свои контексты. [PR-workflow](../../.github/workflows/pr-checks.yml) · [Условные задания](../../.github/workflows/conditional-checks.yml)

## Включение администратором

Администратору для чтения нужно разрешение `Administration: read`, для применения — `Administration: write`.

1. Откройте тестовый PR на ревизии с новыми workflow. Проверьте точное написание двух итоговых имён в Checks и источник GitHub Actions. Для PR только с изменением `docs/release/` оба итога должны быть зелёными, а нативные задания клиентов и нагрузки — пропущенными.
2. Под административной учётной записью сохраните текущую защиту и проверьте применимые rulesets, включая унаследованные. Если участник с подтверждёнными административными правами получает 404 для классической защиты, проверьте rulesets отдельно. Учитывайте существующие более строгие ограничения при подготовке обновления. REST PUT заменяет классическую конфигурацию ветки целиком.

   ```sh
   repo=qlolp/openradioinspector
   gh api "repos/$repo/branches/main/protection" > main-protection.before.json
   gh api --paginate "repos/$repo/rulesets?includes_parents=true"
   gh api "repos/$repo/rules/branches/main"
   ```

3. Примените согласованную политику к `main` в Settings → Branches или через REST. В JSON все обязательные имена привязаны к источнику через `checks`. Пустой `contexts` предусмотрен схемой REST для списка имён без привязки к приложению.

   ```sh
   policy=docs/release/main-protection.json
   gh api --method PUT "repos/$repo/branches/main/protection" --input "$policy"
   gh api "repos/$repo/branches/main/protection" > main-protection.after.json
   jq -e --slurpfile wanted "$policy" '
     (.required_status_checks.strict == true)
     and (($wanted[0].required_status_checks.checks - .required_status_checks.checks) == [])
     and (.enforce_admins.enabled == true)
     and (.required_pull_request_reviews.required_approving_review_count == 0)
     and (.required_pull_request_reviews.dismiss_stale_reviews == true)
     and (.required_conversation_resolution.enabled == true)
     and (.allow_force_pushes.enabled == false)
     and (.allow_deletions.enabled == false)
   ' main-protection.after.json
   ```

4. На временном PR внесите контролируемую ошибку в тест затронутого клиента. Дождитесь красного `PR / Modern clients`; убедитесь в интерфейсе GitHub, что слияние заблокировано именно этой обязательной проверкой. Зафиксируйте URL PR, HEAD и результат `gh pr checks <номер> --repo qlolp/openradioinspector --required`. Посторонняя метка должна сохранить блокировку. Исправьте тест новым коммитом и проверьте, что на новом HEAD все обязательные проверки зелёные. Подтверждением служат успешное чтение административной конфигурации и наблюдаемая блокировка тестового PR.
