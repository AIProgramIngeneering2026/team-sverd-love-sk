# BookScout — core skeleton

Минимальный каркас ядра: Job, ToolRegistry, Dispatcher, Tools, in-memory repository и первые тесты. Ядро не зависит от PySide6, HTTPX, SQLAlchemy, SQLite или другого фреймворка/драйвера БД.

## Требования

- Python 3.11+
- pytest

## Установка и запуск

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

Демонстрационный запуск:

```bash
python -m bookscout.app
```

## Архитектура

- `bookscout/core/job.py` — Job и единственное место, где разрешаются переходы состояний.
- `bookscout/core/registry.py` — поиск Tool по имени, schema-проверка входа и конфигурация workflow.
- `bookscout/core/dispatcher.py` — выполнение workflow через Registry; конкретные Tool Dispatcher неизвестны.
- `bookscout/core/repository.py` — интерфейс хранилища и реализация в памяти.
- `bookscout/tools/builtin.py` — ValidateIsbnTool, ResolveBookTool, SearchStoreOffersTool, NormalizeOffersTool и демонстрационный адаптер.

Новый Tool добавляется регистрацией экземпляра и включением его имени в конфигурацию workflow. Код Dispatcher менять не требуется.

## Валидация

Registry/вызывающий слой проверяет форму входа (обязательные поля, типы, ограничения длины) до создания Job. Tool проверяет доменные инварианты. Например, `ValidateIsbnTool` проверяет контрольную сумму ISBN самостоятельно.

## Злодейства З1–З6

- З1: `Job.tool_execution()` не допускает параллельную работу двух Tool в одной Job.
- З2: startup recovery переводит нетерминальные Job в `FAILED / APP_RESTARTED`.
- З3: `CANCELLED` терминально; поздний результат не может воскресить Job.
- З4: `FAILED` терминально; второй итог запрещён.
- З5: `SUCCEEDED` достижим только из `NORMALIZING`.
- З6: успешная терминальная Job не может вернуться в работу.

Все шесть сценариев применимы и имеют отдельные тесты.

## Ограничения текущего каркаса

- Хранилище in-memory: после завершения процесса Job теряются. Поэтому реальный restart recovery заработает после подключения постоянного Repository; тест имитирует перезапуск Dispatcher при сохранённом repository.
- `InMemoryStoreAdapter` возвращает демонстрационное предложение с `example.org`, это не реальный магазин и не актуальная цена.
- Реальный HTTP-адаптер, сетевые timeout 8 секунд и общий deadline 15 секунд будут подключены отдельно.
