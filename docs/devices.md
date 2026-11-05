# Поддерживаемое оборудование

## Транспортные протоколы

| Схема | Описание | Модуль |
|---|---|---|
| `scpi` | SCPI по TCP/IP (порт 5025, Telnet-подобный или raw-socket) | `devices/scpi.py` |
| `vxi11` | VXI-11 через TCP/IP | `devices/scpi.py` (`Vxi11Instrument`) |
| `serial` | SCPI через последовательный порт (RS-232/USB) | `devices/serial_scpi.py` |
| `soapy` | Универсальный интерфейс SoapySDR | `devices/soapy.py` |
| `rtl` | RTL-SDR через `rtl_tcp` | `devices/rtl.py` |
| `sim` | Встроенный генератор измерений | `devices/simulator.py` |

## Программный стенд общего сеанса

Модуль `device_workers.py` дополняет прежний генератор панорам двумя явными адаптерами нового `/api/v1` контура:

| Адаптер | Результат | Управление |
|---|---|---|
| `tinysa-sim` | Непрерывные свипы, целые Гц и исходные `dBm`; `generator_output=off` | Независимые запуск и остановка потока |
| `ardv1-sim` | Наблюдения с исходными `s_meter_count`, шумоподавителем и имитацией цифрового режима | Независимые запуск, остановка и перестройка через очередь |
| `tinysa-serial` | Архивные свипы с целыми Гц и исходными `dBm`; identity, безопасный setup, `scan` | Явный порт, отдельный transport, `output off`, закрытие |
| `ardv1-serial` | Наблюдения с исходными `s_meter_count`, FM/AM, точный RF, LM | Явный порт, перестройка через очередь, EX при закрытии |

Адаптеры выбираются только через `DeviceStream.settings.adapter`. Симуляторы проверяют архив, конкурентную работу, перестройку, изоляцию сбоев и ручной handoff кандидата на частоту AR-DV1 без остановки tinySA. Дополнительные `tinysa-serial` и `ardv1-serial` используют отдельный POSIX-транспорт на worker и выполняют только документированные команды: tinySA — identity, `output off`, input/LNA setup и `scan`; AR-DV1 — RE1/VR/RN, VFA, FM/AM, RF, LM и EX при закрытии. DMR/TETRA и другие цифровые режимы в этом срезе не отправляются. `hardware_control=true`, но `hardware_validation_not_run=true`: последовательность команд и изоляция сбоев проверены scripted transport, а не физическим стендом.

## Модели приборов

Перечень перечислен в разделе «Особенности подключения» руководства
оператора (Книга 6). OpenRadioInspector обращается с ними через общие
драйверы SCPI/SoapySDR, поэтому конкретные модели в коде не перечисляются,
а определяются параметрами подключения.

| Производитель | Модели |
|---|---|
| Keysight / Agilent | HP 8560–8565; Agilent/Keysight CXA, EXA, MXA (N9000A, N9010A, N9020A), N9320B, N9340B; FieldFox N9913A–N9962A и N9342C–N9344C |
| Rohde & Schwarz | ESCI, EB200, FSP3-40, ETL, PR100, EM100, FSH4/FSH8, FSQ3, GSI3, GSI21 |
| Anritsu | MS2711E–MS2724B, MS8911B, MS27102A, U3772 |
| IFR / Marconi | 2394, 2395, 2397 |
| AOR | SR2200, SR2000, 3000A, Alpha, AR-DV1 |
| Icom | R8500, R9500 |
| SignalHound | SA124B, BB60A, BB60C |
| Narda | NRA6000, IDA-3106 |
| Aeroflex | 3250 |
| Rohde & Schwarz / USRP | Ettus USRP B200, B210 |
| Tektronix | RSA306B |
| AV | AV4051 |
| «Кассандра» | MK, TK, C, SO |

### Примеры подключения

```bash
# Agilent/Keysight N9020A по TCP/IP
python3 -m radioinspector scan \
    --connection "scpi://192.168.1.20:5025?instrument=mx&points=801" \
    --start 30e6 --stop 130e6 --bandwidth 100e3 --cycles 60 --output /tmp/data

# Rohde & Schwarz FSQ3 по TCP/IP
python3 -m radioinspector scan \
    --connection "scpi://192.168.1.30:5025?instrument=fsq" \
    --start 30e6 --stop 130e6 --bandwidth 100e3 --cycles 60 --output /tmp/data

# USRP B210 через SoapySDR
python3 -m radioinspector scan \
    --connection "soapy?driver=uhb&serial=..." \
    --start 30e6 --stop 130e6 --bandwidth 100e3 --cycles 60 --output /tmp/data

# RTL-SDR по rtl_tcp
python3 -m radioinspector scan \
    --connection "rtl?host=127.0.0.1&port=1234" \
    --start 88e6 --stop 108e6 --bandwidth 1e6 --cycles 30 --output /tmp/data
```

Полный перечень схем подключения:

```bash
python3 -m radioinspector devices
```

## Ограничения драйверов

* Драйверы реализуют **базовый** набор команд SCPI
  (`FREQ`, `BAND`, `SWE`, `CAL`, `TRAC`, `MKR`, `FORM`, `OUTP`), достаточный
  для панорамирования и снятия трасс. Сервисные подменю конкретных
  моделей (калибровка по внутреннему меню, загрузка таблиц коррекции,
  переключение траекторий) не реализованы — их документация в руководстве
  оригинала приведена в общем виде.
* Автоматическое определение модели и типа прибора не выполняется: параметры
  указываются в строке подключения.
* Работа с приборами, требующими аутентификации (GPIB-контроллер с
  паролем), не поддержана.
