#!/usr/bin/env bash
# Перенос бота с хоста (python main.py + локальный PostgreSQL) в Docker Compose.
# Запускать из корня репозитория от root: bash scripts/docker-migrate.sh [имя_старой_БД]
#
# Что делает:
#  - проверяет, что старый процесс бота уже остановлен (сам его не трогает)
#  - снимает дамп старой базы в /root/backups
#  - пересоздаёт базу в контейнере и восстанавливает в неё дамп
#  - сверяет количество строк по каждой таблице
#  - запускает бота в контейнере
#
# Старая база на хосте не изменяется — она остаётся для отката.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

OLD_DB="${1:-}"
if [ -z "$OLD_DB" ]; then
    OLD_DB=$(sed -n 's|^DATABASE_URL=.*/\([^/?]*\).*$|\1|p' .env)
fi
BACKUP_DIR=/root/backups
DB_PSQL=(docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB"')

if pgrep -f "python.* main\.py" >/dev/null; then
    echo "Старый бот ещё работает (pgrep -af 'main.py'). Останови его и запусти скрипт снова:" >&2
    echo "  pkill -TERM -f '.venv/bin/python main.py'" >&2
    exit 1
fi

echo "=== 1/5: дамп старой базы ${OLD_DB} ==="
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
DUMP="$BACKUP_DIR/${OLD_DB}_$(date +%F_%H%M%S).sql"
su postgres -c "pg_dump --no-owner --no-acl ${OLD_DB}" > "$DUMP"
chmod 600 "$DUMP"
echo "  $DUMP ($(du -h "$DUMP" | cut -f1))"

echo "=== 2/5: чистая база в контейнере ==="
docker compose down -v
docker compose up -d db
for _ in $(seq 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q db)")" = healthy ] && break
    sleep 2
done

echo "=== 3/5: восстановление дампа ==="
"${DB_PSQL[@]}" < "$DUMP" >/dev/null

echo "=== 4/5: сверка количества строк ==="
COUNT_SQL=$(su postgres -c "psql -d ${OLD_DB} -tAc \"select string_agg(format('select %L, count(*) from %I', tablename, tablename), ' union all ') from pg_tables where schemaname='public'\"")
OLD_COUNTS=$(su postgres -c "psql -d ${OLD_DB} -tAc \"${COUNT_SQL}\"" | sort)
NEW_COUNTS=$(echo "$COUNT_SQL" | "${DB_PSQL[@]}" | sort)
echo "$NEW_COUNTS" | sed 's/^/  /'
if [ "$OLD_COUNTS" != "$NEW_COUNTS" ]; then
    echo "Количество строк не совпало — бот в контейнере НЕ запущен. Было:" >&2
    echo "$OLD_COUNTS" >&2
    exit 1
fi

echo "=== 5/5: запуск бота ==="
docker compose up -d --build
sleep 15
docker compose ps
docker compose logs --tail 20 bot

echo
echo "Готово. Логи: docker compose logs -f bot"
echo "Откат: docker compose down && nohup .venv/bin/python main.py >> logs/bot.log 2>&1 &"
