#!/usr/bin/env bash
# Автоматическая установка и настройка TG_NEWS_BOT на чистом Ubuntu.
# Запускать из корня уже склонированного репозитория: bash scripts/install.sh
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
    exit 1
fi

echo "=== 1/6: системные зависимости ==="
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql postgresql-contrib
bash "$REPO_DIR/scripts/setup-swap.sh"

echo "=== 2/6: виртуальное окружение и зависимости Python ==="
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

echo "=== 3/6: база данных PostgreSQL ==="
service postgresql start >/dev/null 2>&1 || true

read -rp "Имя БД/пользователя PostgreSQL для бота [tg_news_bot]: " DB_NAME
DB_NAME=${DB_NAME:-tg_news_bot}
DB_PASSWORD=$(openssl rand -hex 16)

su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_NAME}'\"" | grep -q 1 \
    && echo "  роль ${DB_NAME} уже существует, пропускаю создание" \
    || su - postgres -c "psql -c \"CREATE USER ${DB_NAME} WITH PASSWORD '${DB_PASSWORD}' CREATEDB;\""

su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1 \
    && echo "  база ${DB_NAME} уже существует, пропускаю создание" \
    || su - postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_NAME};\""

DATABASE_URL="postgresql+asyncpg://${DB_NAME}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"

echo "=== 4/6: настройка .env ==="
echo "Дальше нужны токены и id — их можно ввести сейчас или поправить в .env вручную позже."
echo

read -rp "BOT_TOKEN (от @BotFather): " BOT_TOKEN
echo "Какой нейросетью переписывать новости?"
echo "  1) Claude   (Anthropic, console.anthropic.com)"
echo "  2) GPT      (OpenAI, platform.openai.com)"
echo "  3) Gemini   (Google, aistudio.google.com)"
echo "  4) DeepSeek (platform.deepseek.com)"
read -rp "Номер [1]: " AI_CHOICE
case "${AI_CHOICE:-1}" in
    1) AI_PROVIDER=anthropic ;;
    2) AI_PROVIDER=openai ;;
    3) AI_PROVIDER=gemini ;;
    4) AI_PROVIDER=deepseek ;;
    *) echo "Нет такого варианта: ${AI_CHOICE}" >&2; exit 1 ;;
esac
AI_KEY_NAME="${AI_PROVIDER^^}_API_KEY"
read -rp "${AI_KEY_NAME}: " AI_KEY
read -rp "ADMIN_CHAT_IDS (свой Telegram id, через запятую если админов несколько): " ADMIN_CHAT_IDS
read -rp "Тематика канала, например «автомобильные новости» [автомобильные новости]: " BOT_TOPIC
BOT_TOPIC=${BOT_TOPIC:-автомобильные новости}
read -rp "Таймзона (IANA) [Europe/Moscow]: " TIMEZONE
TIMEZONE=${TIMEZONE:-Europe/Moscow}

FIRST_ADMIN_ID=$(echo "$ADMIN_CHAT_IDS" | cut -d',' -f1 | xargs)
echo
echo "TARGET_GROUP_CHAT_ID/TARGET_TOPIC_ID пока не известны — их бот сам подскажет"
echo "командой /get_topic_id, отправленной прямо внутри нужного топика группы"
echo "(см. README, раздел «Ручная настройка Telegram»). Сейчас ставлю заглушку —"
echo "не забудь поправить .env и перезапустить сервис после этого шага."

cat > "$REPO_DIR/.env" <<EOF
BOT_TOKEN=${BOT_TOKEN}
AI_PROVIDER=${AI_PROVIDER}
${AI_KEY_NAME}=${AI_KEY}
ADMIN_CHAT_IDS=${ADMIN_CHAT_IDS}

# Заглушка — обнови после /get_topic_id (см. вывод выше)
TARGET_GROUP_CHAT_ID=${FIRST_ADMIN_ID}

DATABASE_URL=${DATABASE_URL}
TIMEZONE=${TIMEZONE}
DRAFT_INTERVAL_MINUTES=60
BOT_TOPIC=${BOT_TOPIC}
EOF

echo "  .env создан"

echo "=== 5/6: создание таблиц в БД ==="
"$REPO_DIR/.venv/bin/python" -c "
import asyncio
from bot.db.session import init_models
asyncio.run(init_models())
"

echo "=== 6/6: systemd-сервис ==="
SERVICE_USER="${SUDO_USER:-root}"
cat > /etc/systemd/system/tg-news-bot.service <<EOF
[Unit]
Description=TG_NEWS_BOT Telegram bot
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
echo "Готово. Бот запущен как systemd-сервис tg-news-bot."
echo "  Статус:  systemctl status tg-news-bot"
echo "  Логи:    journalctl -u tg-news-bot -f"
echo
echo "Дальше:"
echo "  1. Добавь бота в целевую группу, включи темы (Topics), выдай права"
echo "     администратора: Post Messages + Manage Topics."
echo "  2. Отправь /get_topic_id прямо внутри нужного топика — бот пришлёт"
echo "     TARGET_GROUP_CHAT_ID и TARGET_TOPIC_ID."
echo "  3. Впиши их в ${REPO_DIR}/.env и перезапусти: systemctl restart tg-news-bot"
echo "  4. Напиши боту /start и добавь источники через меню."
