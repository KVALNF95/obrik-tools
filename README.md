# obrik-tools

Утилита одной командой для прошивки и настройки дрона «Обрик» (Matek H743-Slim, PX4 v1.15.4 + dshot_4way).

**Что делает:** прошивка загрузчика → прошивка PX4 → заливка параметров → отключение писка ESC.

## Установка

```bash
# 1. Клонировать в любое удобное место
git clone https://github.com/KVALNF95/obrik-tools.git
cd obrik-tools

# 2. Установить зависимости
bash setup.sh

# 3. Перезагрузить систему (чтобы права dialout применились)
# Войти заново — и можно работать из этой же папки:
python3 obrik_flash.py --steps all
```

## Конфигурация

Перед первым запуском поправьте пути в `obrik_flash.cfg`:

```ini
bootloader   = /путь/к/matek_h743-slim_bootloader.bin
firmware     = /путь/к/px4/build/matek_h743-slim_default/matek_h743-slim_default.px4
params_file  = /путь/к/Optical_flow_ros2.params
px4_tools    = /путь/к/px4/Tools
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

# Проверить файлы и зависимости без прошивки:
python3 obrik_flash.py --dry-run

# Посмотреть конфиг:
python3 obrik_flash.py --list
```

## Шаги

| Шаг | Действие | Требует | Интерактив |
|-----|----------|---------|------------|
| 1 | Прошивка загрузчика (DFU) | Кнопка BOOT | Зажать BOOT, подключить USB |
| 2 | Прошивка PX4 | USB | Подключить USB (без BOOT) |
| 3 | Параметры (MAVLink param_set) | USB | Автомат, с верификацией критических параметров |
| 4 | Beacon Delay = Infinite | **АКБ** + USB | Beacon пишется в ESC, авто-повторы при ошибке |

## Требования

- ОС: Ubuntu 22.04+ (или любой Linux с Python 3 и dfu-util)
- Полётник: Matek H743-Slim
- Прошивка: PX4 v1.15.4 с драйвером `dshot_4way`
- Регуляторы: BLHeli_S / Bluejay на SiLabs EFM8

## Замечания

- **Перед шагами 2, 3, 4 закройте QGroundControl** — он перехватывает USB-порт.
- Beacon пишется в ESC с авто-повторами (до 3 попыток при `no bootloader response`).
- Проверка АКБ — предупреждение, не блокировка (скрипт продолжит даже без батареи).
- Заливка параметров (шаг 3) — автомат через MAVLink `param_set` + `param save`, с проверкой критических параметров. После неё **обязательно перезагрузите дрон** (отключите и подключите питание).
