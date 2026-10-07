# The everyday commands for the Press. `make` or `make help` lists them.
# Everything here is a thin wrapper: the Press itself is docker compose, and
# the tools are `uv run`.

.DEFAULT_GOAL := help
.PHONY: help up down restart logs status health reprint mail update kindle sample test check

help: ## List these commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-9s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Build and start the Press (the whole thing; needs only Docker)
	docker compose up -d --build
	@echo "The Press is starting. Open http://localhost:$(or $(PRESS_PORT),8484)/ to see the paper."

down: ## Stop the Press; papers already printed are kept
	docker compose down

restart: ## Restart the Press, picking up changes to .env
	docker compose up -d --force-recreate

logs: ## Follow the fetcher's log
	docker compose logs -f press-fetch

status: ## Did today's paper print, and has the Kindle collected it?
	@docker compose exec press-server python -m mnn paper_status

health: ## Is the Press itself working?
	@docker compose exec press-server python -m mnn paper_health

reprint: ## Print today's paper again
	@docker compose exec press-server python -m mnn paper_rebuild

mail: ## Send the newest paper by email now, if it has not gone (see docs/press.md)
	@docker compose exec press-fetch python -m mnn mail

update: ## Pull the latest version and restart
	git pull --ff-only
	docker compose up -d --build

# The Kindle reaches the Press by this machine's address on the network, not
# by localhost, so that is the address the installer has to carry.
PRESS ?= http://$(shell ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1):$(or $(PRESS_PORT),8484)
INSTALLER = $(KINDLE)/documents/Install MNN.sh

kindle: ## Put the installer on a Kindle plugged in over USB: make kindle KINDLE=/path/to/Kindle
	@test -n "$(KINDLE)" || { echo "Say where the Kindle is mounted: make kindle KINDLE=/path/to/Kindle"; exit 1; }
	@test -d "$(KINDLE)/documents" || { echo "$(KINDLE) has no documents folder: is the Kindle mounted there?"; exit 1; }
	curl -fsS -o "$(INSTALLER)" "$(PRESS)/kindle/install.sh?download"
	@grep -q '^PRESS="$(PRESS)"$$' "$(INSTALLER)" || { rm -f "$(INSTALLER)"; echo "The installer from $(PRESS) does not carry that address. Name the Press: make kindle KINDLE=... PRESS=http://<press-address>:8484"; exit 1; }
	@echo "Saved as documents/Install MNN.sh, reading from $(PRESS)."
	@echo "Eject the Kindle. In KOReader's file browser, long-press Install MNN.sh and choose Execute."

sample: ## Build the sample edition into out/ without Docker (needs uv)
	uv run mnn-press build edition/sample.json

test: ## Run the tests (needs uv)
	uv run pytest

check: test ## Tests plus shellcheck, as CI runs them
	git ls-files -z 'kindle/*.sh' | xargs -0 -r shellcheck --shell=sh
	git ls-files -z '*.sh' ':!kindle/*.sh' | xargs -0 -r shellcheck
