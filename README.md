# obrik-tools

Утилита одной командой для прошивки и настройки дрона «Обрик» (Matek H743-Slim, PX4 v1.15.4 + `dshot_4way`).

**Что делает:** mass-erase → загрузчик → прошивка PX4 → параметры → отключение писка ESC. На каждом шаге — реальная проверка результата (см. таблицу). Прошивка PX4 умеет и напрямую через DFU (`.bin`), и через `px_uploader` (`.px4`).

**Автономность:** всё вложено — прошивка (`.px4` и `.bin`), загрузчик, `px_uploader.py`/`mavlink_shell.py`, параметры. Клонировать репозиторий PX4 НЕ нужно.

## Установка

Вариант A — из релизной зипки (для тех, кто просто шьёт):

```bash
unzip obrik-flash-*.zip -d obrik-tools
cd obrik-tools
bash setup.sh          # dfu-util + pymavlink + права dialout
# перезайти в систему (чтобы применились права dialout)
python3 obrik_flash.py --steps all
```

Вариант B — из git (для разработки):

```bash
git clone https://github.com/KVALNF95/obrik-tools.git
cd obrik-tools
bash setup.sh
python3 obrik_flash.py --steps all
```

## Раскладка

```
obrik_flash.py            утилита
obrik_flash.cfg           конфиг (пути относительно этой папки; можно абсолютные)
firmware/  *.px4 *.bin *.bin(bl)   прошивка (px4+bin) и загрузчик
tools/     *.py           вендоренные px_uploader / mavlink_shell (BSD-3, PX4)
params/    *.params       наборы параметров (дефолт — obrik_last.params)
make_release.sh           собрать раздаточную зипку в dist/
```

Конфиг ищется автоматически (`obrik_flash.cfg` рядом со скриптом), либо явно: `-c путь.cfg`.

## Запуск

```bash
python3 obrik_flash.py --steps all      # 1→2→3→4 (загрузчик, прошивка, параметры, beacon)
python3 obrik_flash.py --steps 1,2      # только прошивка
python3 obrik_flash.py --steps 3,4      # параметры + beacon (прошивка уже стоит)
python3 obrik_flash.py --steps params   # только параметры
python3 obrik_flash.py --steps beacon   # только beacon
python3 obrik_flash.py --steps erase,1,2  # mass-erase + полная прошивка (заводской ArduPilot)
python3 obrik_flash.py --dry-run        # проверить файлы и зависимости без прошивки
python3 obrik_flash.py --list           # показать конфиг
```

## Шаги и проверки

| Шаг | Действие | Требует | Проверка результата |
|-----|----------|---------|---------------------|
| 0 | Mass-erase (полное стирание flash) | Кнопка BOOT | `returncode` + **readback**: регион читается как `0xFF` |
| 1 | Загрузчик (DFU) | Кнопка BOOT | `returncode` + **readback** записи, побайтная сверка с файлом |
| 2 | Прошивка PX4 (DFU `.bin` или `px_uploader` `.px4`) | USB / BOOT | реконнект по MAVLink: **heartbeat** (ловит boot-loop) + `AUTOPILOT_VERSION`, сверка git-хэша |
| 3 | Параметры (MAVLink) | USB | **readback каждого** параметра (ответный `PARAM_VALUE`), сверка значения, до 3 ретраев, проверка `param save` |
| 4 | Beacon Delay = Infinite | **АКБ** + USB | подтверждение `ESC N: OK` по каждому ESC, авто-повторы при `no bootloader` |

Шаги 0–3 возвращают успех только если верификация прошла; иначе печатают, что именно не так.

**Шаг 2** автоматически выбирает способ:
- плата в DFU (после шага 1) — прошивает `.bin` напрямую на `app_address` (`0x08020000`);
- плата запущена (ttyACM) — через `px_uploader.py`.

**Шаг 0 (mass-erase)** нужен, когда обычная прошивка не срабатывает — например, на новых платах с заводским ArduPilot (`--steps erase,1,2`).

## Сборка релиза

```bash
./make_release.sh                    # зипка из текущих файлов репы → dist/
./make_release.sh --fw new.px4 --bl new_bl.bin   # подложить свежий билд из форка и упаковать
```

Прошивка собирается из форка PX4 с драйвером `dshot_4way` (нужен для отключения писка ESC). Форк — источник `.px4`/`.bin`; сюда кладётся уже собранный образ.

## Требования

- ОС: Ubuntu 22.04+ (или любой Linux с Python 3 и dfu-util)
- Полётник: Matek H743-Slim
- Регуляторы: BLHeli_S / Bluejay на SiLabs EFM8

## Замечания

- **Перед шагами 2 (px_uploader), 3, 4 закройте QGroundControl** — он перехватывает USB-порт (скрипт сам пытается его прибить).
- При прошивке через DFU (шаг 2 после шага 1) QGroundControl не мешает.
- **Если плата из коробки с ArduPilot** (USB ID `1209:5740`) — выполните `--steps erase,1,2`.
- Если `dfu-util` не поддерживает `mass-erase`/readback — скрипт откатывается/предупреждает, шаг не падает зря.
- После шага 3 **перезагрузите дрон** (отключите и подключите питание).
