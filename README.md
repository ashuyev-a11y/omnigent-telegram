# omnigent-telegram

Telegram-бот — пульт управления оркестратором Omnigent. Один Telegram-чат
привязан к одному проекту (рабочей директории); бот запускает в нём
`omnigent run <bundle_path> -p "<текст задачи>"` и пересылает в чат
URL сессии и финальный вывод.

Это не интерфейс для сложного кодинга (для этого есть веб-UI Omnigent) —
только способ отдать короткую задачу с телефона и получить результат.

## Установка

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфигурация

1. Скопируйте пример конфига и отредактируйте под себя:

   ```bash
   cp config.example.yaml config.yaml
   ```

   ```yaml
   bundle_path: /home/deploy/omnigent-agents/orchestrator
   timeout_seconds: 1800

   projects:
     - name: orchestrator
       chat_id: -1001234567890
       workdir: /home/deploy/omnigent-workspaces/orchestrator-repo
       allowed_user_ids: [123456789]
   ```

   - `bundle_path` — путь к бандлу оркестратора, общий для всех проектов.
   - `timeout_seconds` — таймаут выполнения одной задачи.
   - `state_file` — опционально, путь к JSON-файлу, в котором бот хранит id
     последней omnigent-сессии для каждого чата (по умолчанию
     `./state.json`). Запись атомарная: сначала во временный файл рядом с
     целевым, затем `os.replace()` поверх него — так падение процесса
     посреди записи никогда не оставит файл состояния битым.
   - `projects[].chat_id` — id Telegram-чата, привязанного к проекту
     (уникален в пределах конфига).
   - `projects[].workdir` — рабочая директория, в которой запускается
     `omnigent run` для этого чата.
   - `projects[].allowed_user_ids` — id пользователей, которым разрешено
     писать боту в этом чате. Сообщения от всех остальных чатов и
     пользователей игнорируются без ответа.

   `config.yaml` и `state.json` в `.gitignore` — не коммитьте их, в них
   реальные пути, id и идентификаторы сессий.

2. По умолчанию бот ищет `./config.yaml`. Другой путь можно задать через
   переменную окружения `OMNIGENT_TG_CONFIG`.

3. Токен бота передаётся **только** через переменную окружения
   `TELEGRAM_BOT_TOKEN`. Никогда не кладите токен в `config.yaml` или в
   аргументы командной строки.

## Как узнать chat_id

Проще всего:

1. Добавьте бота в нужный чат (или напишите ему в личные сообщения).
2. Отправьте туда любое сообщение.
3. Запросите обновления через Bot API:

   ```bash
   curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
   ```

4. В ответе найдите `message.chat.id` — это и есть искомый `chat_id`
   (для групп он отрицательный).

Свой `user_id` можно узнать тем же способом (`message.from.id`), либо
написав любому боту вида `@userinfobot`.

## Запуск

```bash
export TELEGRAM_BOT_TOKEN="123456:AAAA..."
export OMNIGENT_TG_CONFIG=/path/to/config.yaml   # опционально
python bot.py
```

Бот работает через long polling — вебхуки не используются, открывать порт
наружу не нужно.

## Команды

- `/start` — краткая справка и имя проекта, привязанного к чату.
- `/status` — выполняется ли сейчас задача в этом чате и URL её сессии; если
  задача не выполняется, показывает id продолжаемой сессии (если она есть)
  либо сообщает, что следующее сообщение начнёт новый разговор.
- `/cancel` — прервать текущую задачу этого чата.
- `/new` — сбросить сохранённую сессию чата: следующее сообщение начнёт
  разговор с чистого листа вместо продолжения предыдущего.

Обычное текстовое сообщение (без команды) в известном чате от разрешённого
пользователя запускает новую задачу — если в чате уже что-то выполняется,
бот ответит, что занят, со ссылкой на текущую сессию.

Начиная с этой версии такое сообщение продолжает предыдущую omnigent-сессию
этого чата (`omnigent run ... --resume <id>`), а не запускается с чистого
листа каждый раз — id сессии сохраняется в `state_file` после каждого успешно
принятого запуска. Если продолжение не удалось (сессия устарела или была
удалена на сервере — ненулевой код возврата не по причине таймаута или
отмены), бот сам сбрасывает сохранённую сессию, сообщает об этом в чат и
просит повторить задачу; повторная отправка начнёт новую сессию.

## systemd-юнит

Пример `/etc/systemd/system/omnigent-telegram.service`:

```ini
[Unit]
Description=omnigent-telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/home/deploy/omnigent-workspaces/telegram-bot
Environment=OMNIGENT_TG_CONFIG=/home/deploy/omnigent-workspaces/telegram-bot/config.yaml
EnvironmentFile=/home/deploy/omnigent-workspaces/telegram-bot/telegram-bot.env
ExecStart=/home/deploy/omnigent-workspaces/telegram-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`telegram-bot.env` (не коммитить, доступ только владельцу) содержит:

```
TELEGRAM_BOT_TOKEN=123456:AAAA...
```

Включение и запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now omnigent-telegram.service
sudo journalctl -u omnigent-telegram.service -f
```

## Логи

Бот логирует факт запуска задачи, `chat_id`, код возврата и длительность
выполнения. Текст задачи целиком и переменные окружения в логи не попадают.

## Тесты

```bash
python -m pytest -q
```

Тесты не требуют токена, сети или установленного Omnigent — подпроцесс в
`test_runner.py` подменяется.

## Ограничения этой версии

Нет approval-карточек и кнопок, нет параллельных задач в одном чате, нет
доступа к Supabase/Vercel/ключам проектов, нет автоматического определения
проекта по тексту, нет вебхуков (только long polling).
