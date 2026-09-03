#!/bin/sh
# Внутренняя часть проверки восстановления: исполняется в контейнере backup,
# где одновременно есть pg_restore нужной версии и том с дампами.
#
# Отдельным файлом, а не строкой в `docker compose run`: вложенные кавычки
# в двух уровнях shell превращают такую строку в нечитаемое место, где
# ошибка находится только запуском.

set -eu

SCRATCH_DB="${SCRATCH_DB:-jarvis_restore}"

DUMP=$(ls -1t /backups/jarvis-*.dump 2>/dev/null | head -1 || true)
if [ -z "$DUMP" ]; then
    echo "локальных дампов нет - сначала make backup" >&2
    exit 1
fi

echo "восстанавливаю $DUMP ($(wc -c < "$DUMP") байт)"

export PGPASSWORD="$POSTGRES_PASSWORD"
PSQL="psql --host=$POSTGRES_HOST --port=$POSTGRES_PORT --username=$POSTGRES_USER --no-password"

# Пересоздаётся каждый прогон: проверка должна показывать состояние дампа,
# а не накопленную историю прошлых проверок.
$PSQL --dbname=postgres -qc "DROP DATABASE IF EXISTS $SCRATCH_DB"
$PSQL --dbname=postgres -qc "CREATE DATABASE $SCRATCH_DB"

# --exit-on-error обязателен: без него pg_restore досыпает ошибки в вывод
# и возвращает ноль, из-за чего битый дамп выглядит восстановленным.
pg_restore --host="$POSTGRES_HOST" --port="$POSTGRES_PORT" --username="$POSTGRES_USER" \
    --no-password --dbname="$SCRATCH_DB" --exit-on-error "$DUMP"

echo "--- таблицы в восстановленной базе ---"
$PSQL --dbname="$SCRATCH_DB" -c '\dt'

echo "--- проба восстановления ---"
if $PSQL --dbname="$SCRATCH_DB" -tAc 'SELECT count(*) FROM restore_probe' 2>/dev/null; then
    echo "строк в restore_probe - выше; данные восстановились, а не только схема"
else
    echo "таблицы restore_probe нет: до Э2 её кладёт infra/restore-probe.sql"
fi
