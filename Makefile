.PHONY: up down test logs build shell migrate

# Start all services in detached mode
up:
	docker-compose up -d

# Stop and remove containers (preserve volumes)
down:
	docker-compose down

# Run the full test suite
test:
	docker-compose run --rm --no-deps api \
		python -m pytest tests/ -v --tb=short

# Tail logs from all services (Ctrl-C to stop)
logs:
	docker-compose logs -f

# Build the API image
build:
	docker-compose build api

# Open a shell in the running API container
shell:
	docker-compose exec api bash

# Apply DB migrations (run after first `make up`)
migrate:
	docker-compose exec postgres \
		psql -U $${POSTGRES_USER:-quorbit} -d $${POSTGRES_DB:-quorbit} \
		     -f /docker-entrypoint-initdb.d/001_pgvector.sql
