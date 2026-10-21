import sys

# Проверка до импорта зависимостей: на старом Python aiogram/pydantic падают с непонятной ошибкой.
if sys.version_info < (3, 11):
    sys.exit(
        "GoodServiceBot требует Python 3.11 или новее, а запущен Python {}.{} ({}).\n"
        "Установите Python 3.11+ с python.org, удалите папку .venv и создайте её заново:\n"
        "    py -3.13 -m venv .venv\n"
        "    .venv\\Scripts\\pip install -r requirements.txt".format(
            sys.version_info[0], sys.version_info[1], sys.executable
        )
    )
