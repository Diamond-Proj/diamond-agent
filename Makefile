# Diamond Agent — local workflow shortcuts (uv-native project workflow).
#   make setup          uv sync (core deps) + .env
#   make setup-live     + globus-compute-sdk             make setup-ai    + anthropic
#   make setup-all      every extra (live + ai)
#   make live           run core + web UI                make ui          open the browser
#   make stop | status  stop / show the running service on $(PORT)
#   make core           run diamond-agent-core headless (API only, for other agents)
#   make cli ARGS=...   diamond-agent-core CLI           make mcp         stdio MCP server
#   make login | endpoints | init-sites | preflight | smoke SYSTEM=Delta@NCSA
#   make clean-state    remove local runtime state (sqlite db)
#
# Note: `uv sync` makes the env exactly match the requested extras — use
# setup-all if you need live and ai together. Run targets use --no-sync so
# they never silently strip extras you installed.

RUN     := uv run --no-sync
PORT    ?= 8642
SITES   ?= sites.live.yaml
SYSTEM  ?= Delta@NCSA

.PHONY: setup setup-live setup-ai setup-all live core stop status cli mcp ui login whoami endpoints \
        init-sites preflight smoke clean-state

setup:
	uv sync
	@test -f .env || (cp .env.example .env && echo "created .env — edit as needed")
	@echo "next: make login && make endpoints && make init-sites && make preflight && make live"

setup-live:
	uv sync --extra live
	@test -f .env || cp .env.example .env

setup-ai:
	uv sync --extra ai
	@test -f .env || cp .env.example .env

setup-all:
	uv sync --all-extras
	@test -f .env || cp .env.example .env

# ---- run ---------------------------------------------------------------

core:   ## headless diamond-agent-core: /api + /docs, no web UI
	$(call port_guard,core)
	@echo "agent skill:  $(CURDIR)/.claude/skills/diamond-agent-core/SKILL.md"
	@echo "              also served at http://127.0.0.1:$(PORT)/skill  (API ref: /skill/api, queues: /api/status?format=text)"
	DIAMOND_AGENT_UI=0 DIAMOND_AGENT_SITES=$(SITES) $(RUN) uvicorn diamond_agent.main:app --port $(PORT)

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
	DIAMOND_AGENT_SITES=$(SITES) $(RUN) uvicorn diamond_agent.main:app --port $(PORT)

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
