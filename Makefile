# Управление ботом в Docker. Список команд: make (или make help)

COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help up down restart deploy update status logs logs-db logs-backup shell psql backup restore

help: ## Показать список команд
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Запустить бота и базу
	$(COMPOSE) up -d

down: ## Остановить всё (данные в базе сохраняются)
	$(COMPOSE) down

restart: ## Перезапустить бота (подхватывает изменения в .env)
	$(COMPOSE) up -d --force-recreate bot

deploy: ## Пересобрать образ и перезапустить бота (после изменения кода)
	$(COMPOSE) up -d --build

update: ## Забрать свежий код из git и развернуть
	git pull --ff-only
	$(COMPOSE) up -d --build

status: ## Состояние контейнеров
	$(COMPOSE) ps

logs: ## Логи бота в реальном времени (выход — Ctrl+C)
	$(COMPOSE) logs -f --tail 100 bot

logs-db: ## Логи базы в реальном времени (выход — Ctrl+C)
	$(COMPOSE) logs -f --tail 100 db

shell: ## Открыть консоль внутри контейнера бота
	$(COMPOSE) exec bot sh

psql: ## Открыть консоль PostgreSQL
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

logs-backup: ## Логи резервного копирования
	$(COMPOSE) logs --tail 50 backup

backup: ## Снять дамп базы сейчас: в backups/ и в S3, если он настроен
	$(COMPOSE) run --rm -T backup backup.sh once

restore: ## Восстановить базу из дампа: make restore FILE=backups/<файл>
	@bash scripts/restore.sh "$(FILE)"

# Локальные цели (в git не входят), например make test
-include Makefile.local
