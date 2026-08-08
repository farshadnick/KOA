.PHONY: init up down logs download status

init:
	./scripts/init.sh

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f app

download:
	./scripts/cli-download.sh $(VERSION)

status:
	curl -s http://localhost:8000/api/status | python3 -m json.tool

serve-offline:
	docker compose -f docker-compose.yml -f docker-compose.serve.yml up -d registry nginx
