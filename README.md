# obrik-tools

Утилита одной командой для прошивки и настройки дрона «Обрик» (Matek H743-Slim, PX4 v1.15.4 + dshot_4way).

**Что делает:** прошивка загрузчика → прошивка PX4 → отключение писка ESC → заливка параметров.

## Установка

```bash
# 1. Клонировать
git clone https://github.com/KVALNF95/obrik-tools.git ~/obrik-tools

# 2. Установить зависимости
cd ~/obrik-tools && bash setup.sh

# 3. Перезагрузить систему (чтобы права dialout применились)
```

## Конфигурация

Перед первым запуском поправьте пути в `obrik_flash.cfg`:

```ini
bootloader   = /home/ИМЯ/Documents/Applications/px4/matek_h743-slim_bootloader.bin
firmware     = /home/ИМЯ/Documents/Applications/px4/PX4-Autopilot/build/matek_h743-slim_default/matek_h743-slim_default.px4
params_file  = /home/ИМЯ/Documents/projects-sverk/params/Optical_flow.params
```

Саму прошивку `.px4` и файл параметров `.params` нужно положить по указанным путям (или скопировать вместе с репозиторием).

## Запуск

```bash
# Всё сразу (загрузчик → прошивка → beacon → параметры):
python3 ~/obrik-tools/obrik_flash.py -c ~/obrik-tools/obrik_flash.cfg --steps all

# Только прошивка:
python3 ~/obrik-tools/obrik_flash.py -c ~/obrik-tools/obrik_flash.cfg --steps 1,2

# Только beacon + параметры (когда прошивка уже стоит):
python3 ~/obrik-tools/obrik_flash.py -c ~/obrik-tools/obrik_flash.cfg --steps 3,4

# Посмотреть конфиг:
python3 ~/obrik-tools/obrik_flash.py -c ~/obrik-tools/obrik_flash.cfg --list
```

## Шаги

| Шаг | Действие | Требует | Интерактив |
|-----|----------|---------|------------|
| 1 | Прошивка загрузчика (DFU) | Кнопка BOOT | Зажать BOOT, подключить USB |
| 2 | Прошивка PX4 | USB | Подключить USB (без BOOT) |
| 3 | Beacon Delay = Infinite | **АКБ** + USB | Beacon пишется в ESC, авто-повторы при ошибке |
| 4 | Параметры (MAVLink param_set) | USB | Автомат, 918 параметров |

## Требования

- ОС: Ubuntu 22.04+ (или любой Linux с Python 3 и dfu-util)
- Полётник: Matek H743-Slim
- Прошивка: PX4 v1.15.4 с драйвером `dshot_4way`
- Регуляторы: BLHeli_S / Bluejay на SiLabs EFM8

## Замечания

- **Перед шагом 2 и 3 закройте QGroundControl** — он перехватывает USB-порт.
- Beacon пишется в ESC с авто-повторами (до 3 попыток при `no bootloader response`).
- Проверка АКБ — предупреждение, не блокировка (скрипт продолжит даже без батареи).
- Заливка параметров (шаг 4) — автомат через MAVLink `param_set` + `param save`. После неё **обязательно перезагрузите дрон** (отключите и подключите питание).
