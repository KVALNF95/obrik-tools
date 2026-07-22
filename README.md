# obrik-tools

Утилита одной командой для прошивки и настройки дрона «Обрик» (Matek H743-Slim, PX4 v1.15.4 + dshot_4way).

**Что делает:** прошивка загрузчика → прошивка PX4 → заливка параметров → отключение писка ESC. Поддерживает прямую заливку прошивки через DFU (без px_uploader).

## Установка

```bash
# 1. Клонировать в любое удобное место
git clone https://github.com/KVALNF95/obrik-tools.git
cd obrik-tools

# 2. Установить зависимости (Linux/macOS)
bash setup.sh

# 3. Перезагрузить систему (чтобы права dialout применились)
# Войти заново — и можно работать из этой же папки:
python3 obrik_flash.py --steps all
```

Windows PowerShell:

```powershell
git clone https://github.com/KVALNF95/obrik-tools.git
cd obrik-tools
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\.venv\Scripts\python.exe .\obrik_flash.py --steps all
```

Для DFU-этапов на Windows нужен `dfu-util.exe` в `PATH`. USB-порт PX4
определяется через `pyserial` (`COMx`), QGroundControl закрывается через
`taskkill`.

## Конфигурация

Перед первым запуском поправьте пути в `obrik_flash.cfg`:

```ini
bootloader   = /путь/к/matek_h743-slim_bootloader.bin
firmware     = /путь/к/px4/build/matek_h743-slim_default/matek_h743-slim_default.px4
firmware_bin = /путь/к/px4/build/matek_h743-slim_default/matek_h743-slim_default.bin
params_file  = /путь/к/Optical_flow_ros2.params
px4_tools    = /путь/к/px4/Tools
app_address  = 0x08020000
```

## Запуск

Из папки, куда клонировали репозиторий:

```bash
# Конфиг ищется автоматически (obrik_flash.cfg рядом со скриптом),
# но можно указать явно: -c /путь/к/конфигу.cfg

# Всё сразу (загрузчик → прошивка → параметры → beacon):
python3 obrik_flash.py --steps all

# Только прошивка:
python3 obrik_flash.py --steps 1,2

# Только параметры + beacon (прошивка уже стоит):
python3 obrik_flash.py --steps 3,4

# Только параметры:
python3 obrik_flash.py --steps params

# Только beacon:
python3 obrik_flash.py --steps beacon

# Mass-erase + полная прошивка (для новых плат с заводским ArduPilot):
python3 obrik_flash.py --steps erase,1,2

# Только mass-erase:
python3 obrik_flash.py --steps erase

# Проверить файлы и зависимости без прошивки:
python3 obrik_flash.py --dry-run

# Посмотреть конфиг:
python3 obrik_flash.py --list
```

## Шаги

| Шаг | Действие | Требует | Интерактив |
|-----|----------|---------|------------|
| 0 | Mass-erase (полное стирание flash) | Кнопка BOOT | Зажать BOOT, подключить USB |
| 1 | Прошивка загрузчика (DFU) | Кнопка BOOT | Зажать BOOT, подключить USB |
| 2 | Прошивка PX4 (DFU или px_uploader) | USB / BOOT | Авто: DFU если плата в DFU, иначе px_uploader |
| 3 | Параметры (MAVLink param_set) | USB + доступное хранилище параметров | ACK каждого параметра, `param save`, reboot и повторное чтение |
| 4 | Beacon Delay = Infinite | **АКБ** + USB | Beacon пишется в ESC, авто-повторы при ошибке |

**Шаг 2** автоматически выбирает способ прошивки:
- Если плата в режиме DFU (после шага 1) — прошивает `.bin` напрямую через `dfu-util` на адрес `0x08020000`. После прошивки нужно отключить и подключить USB заново (без BOOT).
- Если плата запущена (ttyACM) — использует `px_uploader.py` (как раньше).

**Шаг 0 (mass-erase)** нужен когда обычная прошивка не срабатывает — например, на новых платах с заводским ArduPilot. Стирает **всю** flash, включая загрузчик. После mass-erase обязательно выполнить шаги 1 и 2.

## Требования

- ОС: Windows 10/11, Ubuntu 22.04+ или macOS с Python 3 и dfu-util
- Полётник: Matek H743-Slim
- Прошивка: PX4 v1.15.4 с драйвером `dshot_4way`
- Регуляторы: BLHeli_S / Bluejay на SiLabs EFM8

## Замечания

- **Перед шагами 2 (px_uploader), 3, 4 закройте QGroundControl** — он перехватывает USB-порт.
- При прошивке через DFU (шаг 2 после шага 1) QGroundControl не мешает.
- **Если плата из коробки с ArduPilot** (USB ID `1209:5740`, в `lsusb` строка `ArduPilot`) — выполните `--steps erase,1,2` для полной перепрошивки.
- Beacon пишется в ESC с авто-повторами (до 3 попыток при `no bootloader response`).
- Проверка АКБ блокирует шаг 4 при напряжении ниже 3 В: без питания ESC запись невозможна.
- Шаг 3 понимает QGC/PX4-файлы вида `system component NAME VALUE TYPE`, сохраняет реальные типы `INT32/REAL32`, ждёт ACK каждого значения и сам проверяет критические параметры после reboot.
- Перед загрузкой параметров проверяется реальный результат `param save`. Если прошивка хранит параметры в `/fs/microsd/params`, отсутствующая/не смонтированная microSD будет показана как ошибка до загрузки.
