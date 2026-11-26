# Локальный командный интерфейс

BookScout не поднимает HTTP-сервер. `commands.schema.json` описывает JSON-команды CLI, которые передаются Dispatcher; тот же Dispatcher используется PySide6 UI. Для CLI предусмотрен режим `bookscout --json`, одна команда — один JSON-объект на stdin, один JSON-ответ на stdout. Ошибки выводятся в унифицированной оболочке `{"ok":false,"error":{...}}`; диагностические логи идут в stderr.

Операции:

- `books.find` — найти библиографические кандидаты по ISBN или названию.
- `jobs.create` — создать Job для ISBN-13 или подтверждённого `edition_id`.
- `jobs.get` — получить состояние и результаты Job.
- `jobs.cancel` — отменить Job, если переход разрешён state machine.
- `jobs.retry` — создать новую Job для повторного выполнения, с новым `request_key` и `parent_job_id`.

Коды ошибок и exit codes описаны в `../contract-check.md`. HTTP status codes не используются. Семантические классы ошибок: конфликт — `JOB_INVALID_TRANSITION`, `JOB_TERMINAL` или `IDEMPOTENCY_KEY_REUSED` (exit code `4`); ошибка хранилища/внутренняя ошибка — `STORAGE_ERROR` или `INTERNAL_ERROR` (exit code `7`). Полная схема успешной и ошибочной оболочки, перечень кодов и таблица ответов операций находятся в `commands.schema.json`.
