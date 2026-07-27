# Переносимый комплект obrik-tools

## Windows

1. Распаковать архив целиком в любую папку.
2. Установить Python 3 и добавить `dfu-util.exe` в `PATH`.
3. Закрыть QGroundControl.
4. Запустить `INSTALL_WINDOWS.cmd`, затем `RUN_WINDOWS.cmd`.

Windows-комплект уже содержит официальный `dfu-util 0.11`; добавлять его в
`PATH` не требуется. Если Python отсутствует, установщик попробует поставить
Python 3.13 через `winget`.

## macOS/Linux

```bash
chmod +x setup.sh run.sh
Дважды запустить `INSTALL_MAC.command`, затем `RUN_MAC.command`.
```

Отдельные этапы:

```text
RUN_WINDOWS.cmd --steps 1,2
RUN_WINDOWS.cmd --steps params
RUN_WINDOWS.cmd --steps beacon
RUN_WINDOWS.cmd --dry-run
```

На macOS дополнительные аргументы можно передать через Terminal командой
`./RUN_MAC.command --steps params`.

- Шаги 0–1 требуют подключения с зажатой BOOT.
- После прошивки USB переподключается без BOOT.
- Для сохранения параметров требуется исправная microSD.
- Для Beacon Delay нужны USB и подключённый АКБ.
