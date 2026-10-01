#!/usr/bin/env bash
# Восстановление базы из дампа (.sql или .sql.gz). Запускать из корня
# репозитория: make restore FILE=backups/tg_news_bot_....sql.gz
#
# Что делает:
#  - проверяет файл и спрашивает подтверждение
#  - останавливает бота, снимает страховочный дамп текущей базы
#  - пересоздаёт базу и заливает в неё дамп
#  - запускает бота
#
# RESTORE_DB=имя — залить дамп в отдельную базу, чтобы посмотреть его, не
# трогая рабочую: бот при этом не останавливается.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

FILE="${1:-}"
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "Укажи файл дампа: make restore FILE=backups/<файл>" >&2
    echo "Доступные копии:" >&2
    ls -1t backups/*.sql* 2>/dev/null | head -10 >&2 || echo "  (в backups/ пусто)" >&2
    exit 1
fi

case "$FILE" in
    *.gz) gzip -t "$FILE" || { echo "Файл повреждён: $FILE" >&2; exit 1; }; READER=(gunzip -c) ;;
    *) READER=(cat) ;;
esac

# </dev/null: иначе docker exec забирает stdin, и вопрос ниже остаётся без ответа
LIVE_DB=$(docker compose exec -T db sh -c 'printf %s "$POSTGRES_DB"' </dev/null)
TARGET_DB="${RESTORE_DB:-$LIVE_DB}"
if ! [[ "$TARGET_DB" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    echo "Недопустимое имя базы: $TARGET_DB" >&2
    exit 1
fi
db_psql() { docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$0" "$@"' "$@"; }

if [ "$TARGET_DB" = "$LIVE_DB" ]; then
    echo "ВНИМАНИЕ: рабочая база «${LIVE_DB}» будет полностью заменена содержимым"
    echo "  $FILE"
    echo "Бот будет остановлен на время восстановления. Текущая база перед этим"
    echo "сохранится отдельным дампом в backups/."
    read -rp "Продолжить? Введи yes: " answer
    [ "$answer" = "yes" ] || { echo "Отменено."; exit 1; }

    echo "=== 1/4: остановка бота ==="
    docker compose stop bot
    echo "=== 2/4: страховочный дамп текущей базы ==="
    docker compose run --rm -T backup backup.sh once </dev/null
else
    echo "Дамп будет залит в отдельную базу «${TARGET_DB}», рабочая не затрагивается."
fi

echo "=== 3/4: восстановление ==="
db_psql postgres -c "DROP DATABASE IF EXISTS \"${TARGET_DB}\" WITH (FORCE)" -c "CREATE DATABASE \"${TARGET_DB}\"" </dev/null
"${READER[@]}" "$FILE" | db_psql "$TARGET_DB" >/dev/null
echo "Строк по таблицам:"
db_psql "$TARGET_DB" -tA -c "select format('  %s: %s', relname, n_live_tup) from pg_stat_user_tables order by relname" </dev/null || true

if [ "$TARGET_DB" = "$LIVE_DB" ]; then
    echo "=== 4/4: запуск бота ==="
    docker compose up -d
    echo "Готово. Логи: make logs"
else
    echo "Готово. Посмотреть: docker compose exec db psql -U \"\$POSTGRES_USER\" -d ${TARGET_DB}"
    echo "Удалить:    docker compose exec db dropdb -U \"\$POSTGRES_USER\" ${TARGET_DB}"
fi
