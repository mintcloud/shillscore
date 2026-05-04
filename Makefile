.PHONY: help up down logs build rebuild ps psql redis-cli migrate migration shell-api shell-web

COMPOSE = docker compose -f infra/docker-compose.yml --env-file infra/.env

help:
	@echo "shillscore — make targets"
	@echo "  up         start the stack (postgres, redis, api, worker, web)"
	@echo "  down       stop and remove containers"
	@echo "  logs       tail logs from all services"
	@echo "  build      build images"
	@echo "  rebuild    build --no-cache then up -d"
	@echo "  ps         show service status"
	@echo "  psql       psql shell into postgres"
	@echo "  redis-cli  redis-cli shell into redis"
	@echo "  migrate    apply alembic migrations"
	@echo "  migration  m=<msg> create a new alembic revision"
	@echo "  shell-api  bash inside the api container"
	@echo "  shell-web  sh inside the web container"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

build:
	$(COMPOSE) build

rebuild:
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

ps:
	$(COMPOSE) ps

psql:
	$(COMPOSE) exec postgres psql -U shillscore -d shillscore

redis-cli:
	$(COMPOSE) exec redis redis-cli

migrate:
	$(COMPOSE) exec api alembic upgrade head

migration:
	@if [ -z "$(m)" ]; then echo "usage: make migration m=\"add foo\""; exit 1; fi
	$(COMPOSE) exec api alembic revision -m "$(m)"

shell-api:
	$(COMPOSE) exec api bash

shell-web:
	$(COMPOSE) exec web sh
