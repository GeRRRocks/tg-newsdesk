#!/bin/sh
# Резервные копии базы. Работает внутри контейнера backup (см. docker-compose.yml).
#
#   backup.sh loop   — раз в сутки в BACKUP_TIME (режим контейнера по умолчанию)
#   backup.sh once   — одна копия прямо сейчас (make backup)
#
# Копия: pg_dump -> gzip -> /backups, затем отправка в S3, если задан S3_BUCKET.
# Локальные копии старше BACKUP_KEEP_DAYS удаляются; в S3 скрипт ничего не
# удаляет — срок хранения там задаётся правилом жизненного цикла бакета.
# О любой неудаче сообщает админам в Telegram — на языке BOT_LANGUAGE (ru/en);
# журнал контейнера остаётся на русском.

set -eu
set -o pipefail

BACKUP_DIR=/backups
BACKUP_TIME="${BACKUP_TIME:-03:30}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
S3_ENDPOINT="${S3_ENDPOINT:-https://s3.amazonaws.com}"
S3_REGION="${S3_REGION:-us-east-1}"

log() { echo "$(date '+%F %T') | $*"; }

notify_admins() {
    [ -n "${BOT_TOKEN:-}" ] || return 0
    for chat_id in $(echo "${ADMIN_CHAT_IDS:-}" | tr ',' ' '); do
        # Адрес с токеном передаётся через конфиг на stdin, а не аргументом —
        # так он не виден в списке процессов.
        printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$BOT_TOKEN" |
            curl -fsS -m 20 -o /dev/null -K - \
                --data-urlencode "chat_id=${chat_id}" \
                --data-urlencode "text=$1" ||
            log "не удалось отправить сообщение админу ${chat_id}"
    done
}

# fail <текст для журнала и русского сообщения> <текст английского сообщения>
fail() {
    log "ОШИБКА: $1"
    if [ "${BOT_LANGUAGE:-ru}" = en ]; then
        notify_admins "⚠️ Database backup: $2"
    else
        notify_admins "⚠️ Резервная копия базы: $1"
    fi
    return 1
}

upload_s3() {
    file="$1"
    url="${S3_ENDPOINT%/}/${S3_BUCKET}/${S3_PREFIX:-}$(basename "$file")"
    printf 'user = "%s:%s"\n' "$S3_ACCESS_KEY" "$S3_SECRET_KEY" |
        curl -fsS -m 300 --retry 3 --retry-delay 5 -o /dev/null -K - \
            --aws-sigv4 "aws:amz:${S3_REGION}:s3" -T "$file" "$url"
}

run_backup() {
    file="${BACKUP_DIR}/tg_news_bot_$(date +%F_%H%M%S).sql.gz"
    tmp="${file}.tmp"

    if ! PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h db -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$tmp"; then
        rm -f "$tmp"
        fail "не удалось снять дамп." "the dump could not be taken."
        return 1
    fi
    if ! gzip -t "$tmp" || [ "$(wc -c < "$tmp")" -lt 1000 ]; then
        rm -f "$tmp"
        fail "дамп получился пустым или повреждённым." "the dump is empty or corrupted."
        return 1
    fi
    chmod 600 "$tmp" && mv "$tmp" "$file"
    log "дамп сохранён: $(basename "$file") ($(du -h "$file" | cut -f1))"

    if [ -n "${S3_BUCKET:-}" ]; then
        if upload_s3 "$file"; then
            log "отправлено в S3: ${S3_BUCKET}/${S3_PREFIX:-}$(basename "$file")"
        else
            fail "дамп снят, но не отправлен в S3. Локальная копия: $(basename "$file")." \
                "the dump was taken but not uploaded to S3. Local copy: $(basename "$file")."
            return 1
        fi
    else
        log "S3 не настроен (S3_BUCKET пуст) — копия только локальная"
    fi

    find "$BACKUP_DIR" -maxdepth 1 -name 'tg_news_bot_*.sql*' -mtime "+${KEEP_DAYS}" -delete
}

seconds_until_next_run() {
    now=$(date +%s)
    target=$(date -d "$BACKUP_TIME" +%s)
    [ "$target" -gt "$now" ] || target=$((target + 86400))
    echo $((target - now))
}

mkdir -p "$BACKUP_DIR"

case "${1:-loop}" in
    once)
        run_backup
        ;;
    loop)
        log "резервные копии: ежедневно в ${BACKUP_TIME} ($(date +%Z)), хранить ${KEEP_DAYS} дн., S3: ${S3_BUCKET:-не настроен}"
        # Если свежей копии нет (первый запуск или контейнер долго не работал) — не ждём до ночи
        if [ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name 'tg_news_bot_*.sql.gz' -mmin -1560 | head -1)" ]; then
            run_backup || true
        fi
        while true; do
            sleep "$(seconds_until_next_run)"
            run_backup || true
        done
        ;;
    *)
        echo "usage: backup.sh [loop|once]" >&2
        exit 2
        ;;
esac
