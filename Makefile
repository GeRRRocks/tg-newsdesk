# Управление ботом в Docker. Список команд: make (или make help)

COMPOSE := docker compose
BACKUP_DIR := backups

.DEFAULT_GOAL := help
.PHONY: help up down restart deploy update status logs logs-db shell psql backup

help: ## Показать список команд
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

backup: ## Сохранить дамп базы в backups/
	@mkdir -p $(BACKUP_DIR) && chmod 700 $(BACKUP_DIR)
	@file=$(BACKUP_DIR)/tg_news_bot_$$(date +%F_%H%M%S).sql; \
	$(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB"' > $$file.tmp \
		&& mv $$file.tmp $$file && chmod 600 $$file \
		&& echo "Дамп сохранён: $$file ($$(du -h $$file | cut -f1))" \
		|| { rm -f $$file.tmp; echo "Не удалось снять дамп" >&2; exit 1; }
