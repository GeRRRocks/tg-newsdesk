#!/usr/bin/env bash
# Создаёт файл подкачки на сервере с небольшим объёмом памяти.
# Вызывается из `make up` / `make deploy` / `make update` и из scripts/install.sh;
# можно запустить и отдельно: sudo bash scripts/setup-swap.sh
#
# Зачем: бот, PostgreSQL и сборка образа вместе занимают заметную часть памяти
# сервера на 1–2 ГБ. Без подкачки при её нехватке ядро просто убивает процессы.
#
# Ничего не делает, если подкачка уже есть, памяти 4 ГБ и больше, скрипт запущен
# не от root или система не Linux. Развёртывание из-за него не прерывается.
#
# Переменные: SWAP_SIZE_MB (по умолчанию 2048), SWAP_FILE (по умолчанию /swapfile).

set -euo pipefail

SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
SWAP_FILE="${SWAP_FILE:-/swapfile}"
MIN_RAM_MB=4000

skip() {
    echo "swap: $1"
    exit 0
}

[ "$(uname -s)" = Linux ] || skip "не Linux, пропускаю"
[ -r /proc/swaps ] && [ -r /proc/meminfo ] || skip "нет /proc, пропускаю"

if [ "$(tail -n +2 /proc/swaps | wc -l)" -gt 0 ]; then
    skip "подкачка уже включена, ничего не делаю"
fi

ram_mb=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
[ "$ram_mb" -lt "$MIN_RAM_MB" ] || skip "памяти ${ram_mb} МБ, подкачка не нужна"

if [ "$(id -u)" -ne 0 ]; then
    skip "памяти ${ram_mb} МБ и нет подкачки. Запусти от root: sudo bash scripts/setup-swap.sh"
fi

[ -e "$SWAP_FILE" ] && skip "${SWAP_FILE} уже существует, но не подключён — разберись вручную"

# Оставляем на диске запас не меньше 2 ГБ сверх самого файла
free_mb=$(df -Pm "$(dirname "$SWAP_FILE")" | awk 'NR == 2 {print $4}')
if [ "$free_mb" -lt $((SWAP_SIZE_MB + 2048)) ]; then
    skip "на диске свободно ${free_mb} МБ — мало для файла подкачки ${SWAP_SIZE_MB} МБ, пропускаю"
fi

echo "swap: памяти ${ram_mb} МБ, создаю ${SWAP_FILE} на ${SWAP_SIZE_MB} МБ"
# fallocate быстрее, но не на всех файловых системах даёт пригодный для swap файл
if ! fallocate -l "${SWAP_SIZE_MB}M" "$SWAP_FILE" 2>/dev/null; then
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MB" status=none
fi
chmod 600 "$SWAP_FILE"
mkswap "$SWAP_FILE" >/dev/null
if ! swapon "$SWAP_FILE"; then
    rm -f "$SWAP_FILE"
    skip "не удалось включить подкачку (контейнер или файловая система без поддержки), пропускаю"
fi

# Чтобы подкачка пережила перезагрузку
if ! grep -qE "^${SWAP_FILE}[[:space:]]" /etc/fstab; then
    echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
fi

# Подкачка — страховка, а не рабочая память: пользоваться ею только при нехватке
echo "vm.swappiness=10" > /etc/sysctl.d/99-tg-news-bot-swap.conf
sysctl -q -p /etc/sysctl.d/99-tg-news-bot-swap.conf

echo "swap: готово"
