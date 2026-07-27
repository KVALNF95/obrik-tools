# Переносимый комплект obrik-tools

## Windows

1. Распаковать архив целиком в любую папку.
2. Установить Python 3 и добавить `dfu-util.exe` в `PATH`.
3. Закрыть QGroundControl.
4. Запустить `run.cmd`. При первом запуске автоматически создастся `.venv`
   и установятся зависимости из `requirements.txt`.

## macOS/Linux

```bash
chmod +x setup.sh run.sh
./run.sh
```

Отдельные этапы:

```text
run.cmd --steps 1,2
run.cmd --steps params
run.cmd --steps beacon
run.cmd --dry-run
```

На macOS/Linux вместо `run.cmd` используется `./run.sh`.

- Шаги 0–1 требуют подключения с зажатой BOOT.
- После прошивки USB переподключается без BOOT.
- Для сохранения параметров требуется исправная microSD.
- Для Beacon Delay нужны USB и подключённый АКБ.
