# Native monitoring usability

The macOS client keeps hardware ownership and archive writes in the core. This increment adds:

- Explicit local development-core launch and shutdown, with an interpreter picker for prepared Python environments. Personal RC keeps its existing supervised service.
- A graceful stop that finishes the active measurement, and a separate explicit force-stop action. The Swift client checks the advertised capability before requesting graceful stop from an older core.
- Separate core reachability and measurement freshness. A failed refresh retains the last spectrum visibly marked stale; receiver observations cannot make an old analyzer spectrum fresh.
- A full-width analyzer when no receiver is configured, smaller minimum window dimensions, and Russian primary controls and diagnostic explanations.

Raw measurement units, timestamps, archive ownership and generator policy are unchanged. tinySA generation is never enabled by these changes.

## Verification

Swift package tests cover stop negotiation, stale measurements and helper process lifecycle. Python tests cover independent worker shutdown, force escalation, cancellation and tinySA command-boundary recovery. These are synthetic checks, not hardware acceptance.

See [development core](macos-development-core.md) and [tinySA stop/restart](tinysa-stop-restart.md) for operational limits. Hardware results and full-suite results are reported separately in the pull request; private captures and local logs are not committed.

## Работа с новым событием охраны

1. На странице «Охрана» выберите проект. Проверьте свежесть обзора, дождитесь
   подтверждения эталона и включите охрану. Ход контрольной проверки AR-DV1
   отображается в состоянии проекта.
2. На странице «Сигналы» выберите «События охраны». У непросмотренной строки
   есть отметка. Частота, пик, превышение, результат AR-DV1 и время UTC видны
   в таблице. «Кандидаты сеанса» открывает существующий список ручной работы.
3. Выберите строку и нажмите «Исходный свип…». Карточка показывает причину
   срабатывания, эталон, показания приборов, возможные службы и ссылки на
   справочник. Для списка полос в видимой части спектра нажмите подпись
   диапазона над графиком.
4. Если сигнал пригодится при следующей проверке, нажмите «Сохранить в
   каталог…», дайте ему имя и укажите тип. При повторном сохранении карточка
   обновляется. Решение по тревоге остаётся отдельным действием. Найти запись
   можно во вкладке «Известные сигналы» этого проекта.

Каталог ищет совпадения по центральной частоте, поддиапазону и ревизии плана
с учётом ширины срабатывания. Совпадения по справочнику помечены как гипотезы.
Исходные свипы и показания AR-DV1 сохраняются в своих единицах. Данные
симулятора помечены в карточке. [Справочные полосы и API](rt-comparison.md#справочные-полосы-и-журнал-сигналов).

Проверка с новым оператором проводится на заранее подготовленном задании.
Он должен самостоятельно найти активный проект, заметить новое событие,
открыть исходный свип максимум за два действия, объяснить результат AR-DV1
и сохранить известный сигнал. Запишите результат каждого участника. Для
приёмки основной сценарий должны пройти 4 из 5 новых операторов.
