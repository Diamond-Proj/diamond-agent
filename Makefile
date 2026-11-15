# Diamond Agent — local workflow (uv-native). Run `make` for the onboarding steps.
#   make setup          install everything (uv sync --all-extras) + .env
#   make doctor         check what is missing and print the next step
#   make login          Globus login for the standalone core (URL + one-time code)
#   make endpoint-setup HOST=user@login-host   create/start the endpoint ON a supercomputer
#   make endpoints      list your Globus Compute endpoints (known machines flagged)
#   make init-sites     create or update sites.live.yaml from those endpoints (a merge: accounts and hand edits are kept)
#   make preflight      auth → endpoints → probe → scheduler → submit-test gate
#   make live | core    run core + web UI | headless API for other agents
#   make stop | status  stop / show the running service on $(PORT)
#   make channels       channel URLs for agents (ARGS="create NAME" / "delete ID" to manage them without the UI)
#   make cli ARGS=...   diamond-agent-core CLI           make mcp         stdio MCP server
#   make clean-state    remove local runtime state (sqlite db)
#
# Run targets use --no-sync so they never silently change the installed env.

RUN     := uv run --no-sync
PORT    ?= 8642
SITES   ?= sites.live.yaml
SYSTEM  ?= Delta@NCSA

.PHONY: help setup setup-min setup-all doctor endpoint-setup live core stop status channels cli mcp ui login whoami \
        endpoints init-sites preflight smoke clean-state

HOST    ?=
NAME    ?= diamond-agent

help:
	@echo "Diamond Agent — first run:"
	@echo "  1. make setup                              install (uv) + .env"
	@echo "  2. make login                              Globus login (or sign in later from the web UI)"
	@echo "  3. make endpoint-setup HOST=user@login-host  once per supercomputer, creates the Globus Compute endpoint there"
	@echo "  4. make endpoints && make init-sites       create/update sites.live.yaml (accounts are read from Slurm; re-runs keep your edits)"
	@echo "  5. make preflight                          verify the whole chain"
	@echo "  6. make live   (core + web UI)   or   make core   (headless API for other agents)"
	@echo "  7. make channels                           the URL to hand agents (DIAMOND_AGENT_URL); create/delete channels without the UI"
	@echo "Anytime: make doctor  — tells you which step is missing."

setup:          ## everything: SDK + MCP extra + .env
	uv sync --all-extras
	@test -f .env || (cp .env.example .env && echo "created .env — edit as needed")
	@$(MAKE) --no-print-directory doctor

setup-all: setup

setup-min:      ## core only (no MCP server)
	uv sync
	@test -f .env || cp .env.example .env

doctor:         ## onboarding check: prints what is missing and the next step (never prompts)
	@$(RUN) python -m diamond_agent.doctor --sites $(SITES)

endpoint-setup: ## one-time per supercomputer: run the setup script on the login node (ONE ssh connection = one password/MFA)
	@test -n "$(HOST)" || { echo "usage: make endpoint-setup HOST=user@login.delta.ncsa.illinois.edu [NAME=diamond-agent]"; exit 1; }
	bash scripts/endpoint_setup_remote.sh $(HOST) $(NAME)
	@echo "now: make endpoints && make init-sites"

# ---- run ---------------------------------------------------------------

core:   ## headless diamond-agent-core: /api + /docs, no web UI
	$(call port_guard,core)
	@echo "agent skill:  $(CURDIR)/.claude/skills/diamond-agent-core/SKILL.md"
	@echo "              also served at <channel url>/skill  (API ref: /skill/api, queues: /api/status?format=text)"
	@echo "channel URLs: printed below at start; anytime: make channels"
	DIAMOND_AGENT_UI=0 DIAMOND_AGENT_PORT=$(PORT) DIAMOND_AGENT_SITES=$(SITES) $(RUN) uvicorn diamond_agent.main:app --port $(PORT)

stop:   ## stop any diamond-agent-core / uvicorn started from this project (waits for the port)
	@if pkill -f "uvicorn diamond_agent.main:app"; then \
	  for i in 1 2 3 4 5 6 7 8 9 10; do lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t >/dev/null 2>&1 || break; sleep 1; done; \
	  echo "stopped"; else echo "nothing running"; fi

status: ## what is listening on $(PORT) and whether it serves the UI
	@pid=$$(lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t 2>/dev/null | head -1); \
	if [ -z "$$pid" ]; then echo "nothing listening on $(PORT)"; exit 0; fi; \
	echo "pid $$pid: $$(ps -o command= -p $$pid | cut -c1-80)"; \
	curl -s http://127.0.0.1:$(PORT)/ | head -c 200; echo; \
	echo "ui: $$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$(PORT)/static/app.js | sed 's/200/on/; s/404/off/')"

cli:    ## e.g. make cli ARGS="resources" | ARGS="submit --name t --command 'echo hi' --wait"
	$(RUN) python -m diamond_agent.core.cli $(ARGS)

mcp:    ## stdio MCP server proxying to the running core (needs: uv sync --extra mcp)
	$(RUN) python -m diamond_agent.core.mcp_server


# Refuse to start on a busy port: uvicorn would exit and the browser would
# keep showing whatever old server (possibly with the UI) still owns it.
define port_guard
	@pid=$$(lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t 2>/dev/null | head -1); \
	if [ -n "$$pid" ]; then echo "port $(PORT) is already in use by pid $$pid ($$(ps -o command= -p $$pid | cut -c1-70))"; \
	echo "stop it first:  make stop   (or choose another port: make $(1) PORT=8643)"; exit 1; fi
endef

live:
	$(call port_guard,live)
	DIAMOND_AGENT_PORT=$(PORT) DIAMOND_AGENT_SITES=$(SITES) $(RUN) uvicorn diamond_agent.main:app --port $(PORT)

channels: ## channel URLs for agents; ARGS="create NAME [-d TEXT] [--rules FILE.json]" | "delete ID" | "list --json"
	DIAMOND_AGENT_PORT=$(PORT) DIAMOND_AGENT_SITES=$(SITES) $(RUN) python -m diamond_agent.channels $(or $(ARGS),list)

ui:
	open http://127.0.0.1:$(PORT)

# ---- live-mode setup gates --------------------------------------------

login:
	$(RUN) python -m diamond_agent.auth login

whoami:
	$(RUN) python -m diamond_agent.auth whoami

endpoints:
	$(RUN) python -m diamond_agent.auth endpoints

init-sites:
	$(RUN) python -m diamond_agent.auth init-sites --out $(SITES)

preflight:
	$(RUN) python -m diamond_agent.preflight --sites $(SITES)

smoke:
	$(RUN) python -m diamond_agent.preflight --sites $(SITES) --submit-test $(SYSTEM)

# ---- housekeeping ------------------------------------------------------

clean-state:   ## remove local runtime state (keeps Globus tokens and the catalog)
	rm -f diamond_agent.db
