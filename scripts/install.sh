#!/usr/bin/env bash
# Автоматическая установка и настройка tg-newsdesk на чистом Ubuntu.
# Automatic installation of tg-newsdesk on a clean Ubuntu server.
# Запускать из корня уже склонированного репозитория: sudo bash scripts/install.sh
#
# Первым делом спрашивает язык (русский или английский): на нём идут все
# дальнейшие вопросы, и он же записывается в .env как BOT_LANGUAGE — язык
# самого бота.
#
# Что делает:
#  - ставит системные зависимости (python3-venv, postgresql)
#  - создаёт файл подкачки, если памяти на сервере мало (scripts/setup-swap.sh)
#  - создаёт venv и ставит зависимости из requirements.txt
#  - поднимает PostgreSQL-базу и пользователя под бота
#  - интерактивно спрашивает токены/тематику и пишет .env
#  - создаёт таблицы в БД
#  - настраивает systemd-юнит, чтобы бот работал в фоне и переживал перезагрузку

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ "$(id -u)" -ne 0 ]; then
    echo "Запусти скрипт от root (или через sudo) — нужно ставить системные пакеты." >&2
    echo "Run the script as root (or with sudo) — it installs system packages." >&2
    exit 1
fi

echo "Язык установки и бота / Installation and bot language:"
echo "  1) Русский"
echo "  2) English"
read -rp "[1]: " LANG_CHOICE
case "${LANG_CHOICE:-1}" in
    1) BOT_LANGUAGE=ru ;;
    2) BOT_LANGUAGE=en ;;
    *) echo "Нет такого варианта / No such option: ${LANG_CHOICE}" >&2; exit 1 ;;
esac
export BOT_LANGUAGE

# m <русский текст> <English text> — печатает текст на выбранном языке
m() {
    if [ "$BOT_LANGUAGE" = en ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi
}

# ask <имя переменной> <русский вопрос> <English question>
ask() {
    local answer
    read -rp "$(m "$2" "$3")" answer
    printf -v "$1" '%s' "$answer"
}

m "=== 1/6: системные зависимости ===" "=== 1/6: system packages ==="
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql postgresql-contrib
bash "$REPO_DIR/scripts/setup-swap.sh"

m "=== 2/6: виртуальное окружение и зависимости Python ===" "=== 2/6: virtual environment and Python dependencies ==="
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

m "=== 3/6: база данных PostgreSQL ===" "=== 3/6: PostgreSQL database ==="
service postgresql start >/dev/null 2>&1 || true

ask DB_NAME "Имя БД/пользователя PostgreSQL для бота [tg_news_bot]: " \
    "PostgreSQL database/user name for the bot [tg_news_bot]: "
DB_NAME=${DB_NAME:-tg_news_bot}
DB_PASSWORD=$(openssl rand -hex 16)

su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_NAME}'\"" | grep -q 1 \
    && m "  роль ${DB_NAME} уже существует, пропускаю создание" "  role ${DB_NAME} already exists, skipping" \
    || su - postgres -c "psql -c \"CREATE USER ${DB_NAME} WITH PASSWORD '${DB_PASSWORD}' CREATEDB;\""

su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1 \
    && m "  база ${DB_NAME} уже существует, пропускаю создание" "  database ${DB_NAME} already exists, skipping" \
    || su - postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_NAME};\""

DATABASE_URL="postgresql+asyncpg://${DB_NAME}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"

m "=== 4/6: настройка .env ===" "=== 4/6: .env configuration ==="
m "Дальше нужны токены и id — их можно ввести сейчас или поправить в .env вручную позже." \
    "Tokens and ids are needed next — enter them now or edit .env by hand later."
echo

ask BOT_TOKEN "BOT_TOKEN (от @BotFather): " "BOT_TOKEN (from @BotFather): "
m "Какой нейросетью переписывать новости?" "Which LLM should rewrite the news?"
echo "  1) Claude   (Anthropic, console.anthropic.com)"
echo "  2) GPT      (OpenAI, platform.openai.com)"
echo "  3) Gemini   (Google, aistudio.google.com)"
echo "  4) DeepSeek (platform.deepseek.com)"
ask AI_CHOICE "Номер [1]: " "Number [1]: "
case "${AI_CHOICE:-1}" in
    1) AI_PROVIDER=anthropic ;;
    2) AI_PROVIDER=openai ;;
    3) AI_PROVIDER=gemini ;;
    4) AI_PROVIDER=deepseek ;;
    *) m "Нет такого варианта: ${AI_CHOICE}" "No such option: ${AI_CHOICE}" >&2; exit 1 ;;
esac
AI_KEY_NAME="${AI_PROVIDER^^}_API_KEY"
read -rp "${AI_KEY_NAME}: " AI_KEY
ask ADMIN_CHAT_IDS "ADMIN_CHAT_IDS (свой Telegram id, через запятую если админов несколько): " \
    "ADMIN_CHAT_IDS (your Telegram id; comma-separated if there are several admins): "
DEFAULT_TOPIC=$(m "автомобильные новости" "car news")
ask BOT_TOPIC "Тематика канала, например «автомобильные новости» [${DEFAULT_TOPIC}]: " \
    "Channel subject, for example “car news” [${DEFAULT_TOPIC}]: "
BOT_TOPIC=${BOT_TOPIC:-$DEFAULT_TOPIC}
ask TIMEZONE "Таймзона (IANA) [Europe/Moscow]: " "Time zone (IANA) [Europe/Moscow]: "
TIMEZONE=${TIMEZONE:-Europe/Moscow}

FIRST_ADMIN_ID=$(echo "$ADMIN_CHAT_IDS" | cut -d',' -f1 | xargs)
echo
m "TARGET_GROUP_CHAT_ID/TARGET_TOPIC_ID пока не известны — их бот сам подскажет
командой /get_topic_id, отправленной прямо внутри нужного топика группы
(см. README, раздел «Настройка Telegram»). Сейчас ставлю заглушку —
не забудь поправить .env и перезапустить сервис после этого шага." \
    "TARGET_GROUP_CHAT_ID/TARGET_TOPIC_ID are not known yet — the bot will tell you
when you send /get_topic_id right inside the target topic of the group
(see README.en.md, “Telegram setup”). A placeholder is written for now —
remember to fix .env and restart the service after that step."

PLACEHOLDER_NOTE=$(m "Заглушка — обнови после /get_topic_id (см. вывод выше)" \
    "Placeholder — update after /get_topic_id (see the output above)")
cat > "$REPO_DIR/.env" <<EOF
BOT_TOKEN=${BOT_TOKEN}
BOT_LANGUAGE=${BOT_LANGUAGE}
AI_PROVIDER=${AI_PROVIDER}
${AI_KEY_NAME}=${AI_KEY}
ADMIN_CHAT_IDS=${ADMIN_CHAT_IDS}

# ${PLACEHOLDER_NOTE}
TARGET_GROUP_CHAT_ID=${FIRST_ADMIN_ID}

DATABASE_URL=${DATABASE_URL}
TIMEZONE=${TIMEZONE}
DRAFT_INTERVAL_MINUTES=60
BOT_TOPIC=${BOT_TOPIC}
EOF
chmod 600 "$REPO_DIR/.env"

m "  .env создан" "  .env created"

m "=== 5/6: создание таблиц в БД ===" "=== 5/6: creating database tables ==="
"$REPO_DIR/.venv/bin/python" -c "
import asyncio
from bot.db.session import init_models
asyncio.run(init_models())
"

m "=== 6/6: systemd-сервис ===" "=== 6/6: systemd service ==="
SERVICE_USER="${SUDO_USER:-root}"
chown "$SERVICE_USER" "$REPO_DIR/.env"
cat > /etc/systemd/system/tg-news-bot.service <<EOF
[Unit]
Description=tg-newsdesk Telegram bot
After=network.target postgresql.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python ${REPO_DIR}/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now tg-news-bot.service

echo
m "Готово. Бот запущен как systemd-сервис tg-news-bot.
  Статус:  systemctl status tg-news-bot
  Логи:    journalctl -u tg-news-bot -f

Дальше:
  1. Добавь бота в целевую группу, включи темы (Topics), выдай права
     администратора: Post Messages + Manage Topics.
  2. Отправь /get_topic_id прямо внутри нужного топика — бот пришлёт
     TARGET_GROUP_CHAT_ID и TARGET_TOPIC_ID.
  3. Впиши их в ${REPO_DIR}/.env и перезапусти: systemctl restart tg-news-bot
  4. Напиши боту /start и добавь источники через меню." \
    "Done. The bot is running as the systemd service tg-news-bot.
  Status:  systemctl status tg-news-bot
  Logs:    journalctl -u tg-news-bot -f

Next:
  1. Add the bot to the target group, enable Topics and make it an admin
     with Post Messages + Manage Topics.
  2. Send /get_topic_id right inside the target topic — the bot replies with
     TARGET_GROUP_CHAT_ID and TARGET_TOPIC_ID.
  3. Put them into ${REPO_DIR}/.env and restart: systemctl restart tg-news-bot
  4. Send /start to the bot and add sources through the menu."
