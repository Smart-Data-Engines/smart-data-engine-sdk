# One entry point per language, plus the conformance suite that all of them have to pass.
#
# `make check` is what CI runs and what you should run before pushing. It deliberately does not stop
# at the first failure across languages: if Python and TypeScript both diverged from the contract you
# want to see both, because the fix is usually in the contract rather than in either library.

PY := python/.venv/bin

.PHONY: help check python-check python-test python-lint python-types ts-check ts-test ts-types \
        conformance clean pg-up pg-down ch-up ch-down engines-up engines-down

help:
	@echo "check         everything CI runs"
	@echo "python-check  lint, types and tests for the Python library"
	@echo "ts-check      types and tests for the TypeScript library"
	@echo "conformance   run the shared vectors in every language that has them"
	@echo "engines-up    start both engines the integration slices need"
	@echo "engines-down  stop them"
	@echo "pg-up/ch-up   start one of them"

# Both languages, and deliberately not stopping at the first failure across them: if Python and
# TypeScript have both drifted from the contract you want to see both, because the fix is usually in
# the contract rather than in either library.
check: python-check ts-check

python-check: python-lint python-types python-test

python-lint:
	cd python && .venv/bin/ruff check src tests

python-types:
	cd python && .venv/bin/mypy src

python-test:
	cd python && .venv/bin/python -m pytest

ts-check: ts-types ts-test

ts-types:
	cd typescript && npx tsc --noEmit

ts-test:
	cd typescript && npx vitest run

conformance:
	cd python && .venv/bin/python -m pytest tests/test_conformance.py -v
	cd typescript && npx vitest run tests/conformance.test.ts

# The integration slice runs against a real PostgreSQL rather than a fake, because a fake would agree
# with whatever this library believes about types, quoting and transactions - which is exactly the set
# of beliefs worth checking.
# Both, because the test that matters most needs both at once: one value written through each adapter,
# read back from each, asserted equal in content and in Python type. With only one server running that
# test skips, and a skipped test reads exactly like a passing one.
# Every readiness probe below is bounded, and probes the address the tests use rather than one
# inside the container. Both halves of that come from one incident: `ch-up` waited on
# `docker exec ... wget --spider http://localhost:8123/ping` in an `until` loop with no bound. The
# container's `localhost` resolves to `::1` first and ClickHouse in this image listens on IPv4 only,
# so the probe was refused every second for eighty minutes. Nothing was wrong with the engine -
# `curl http://127.0.0.1:$(CH_PORT)/ping` from the host answered 200 the whole time.
#
# So: an unbounded wait turns a broken probe into an indefinite hang, which reads exactly like a
# slow start and produces no output to read. And a probe that takes a different path to the server
# than the tests do can fail while the tests would pass - or pass while they would fail, which is
# the direction that wastes a debugging session.
READY_TIMEOUT := 60
PG_PORT := 55432
CH_PORT := 58123

engines-up: pg-up ch-up
	@echo
	@echo "export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:$(PG_PORT)/sde"
	@echo "export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:$(CH_PORT)/sde"

engines-down: pg-down ch-down

pg-up:
	@docker start sde_test_pg 2>/dev/null || docker run -d --name sde_test_pg \
		-e POSTGRES_PASSWORD=sde -e POSTGRES_DB=sde \
		-p 127.0.0.1:$(PG_PORT):5432 postgres:15-alpine
	@n=0; until docker exec sde_test_pg pg_isready -q \
		&& python3 -c 'import socket,sys; s=socket.create_connection(("127.0.0.1",$(PG_PORT)),1); s.close()' \
		2>/dev/null; do \
		n=$$((n+1)); \
		if [ $$n -ge $(READY_TIMEOUT) ]; then \
			echo "sde_test_pg was not ready after $(READY_TIMEOUT)s. Probe output:"; \
			docker exec sde_test_pg pg_isready; \
			python3 -c 'import socket; socket.create_connection(("127.0.0.1",$(PG_PORT)),1)' || true; \
			docker logs --tail 20 sde_test_pg; \
			exit 1; \
		fi; \
		sleep 1; \
	done

pg-down:
	-docker rm -f sde_test_pg

ch-up:
	@docker start sde_test_ch 2>/dev/null || docker run -d --name sde_test_ch \
		-e CLICKHOUSE_PASSWORD=sde -e CLICKHOUSE_DB=sde \
		-p 127.0.0.1:$(CH_PORT):8123 clickhouse/clickhouse-server:24.8-alpine
	@n=0; until curl -fs -o /dev/null http://127.0.0.1:$(CH_PORT)/ping; do \
		n=$$((n+1)); \
		if [ $$n -ge $(READY_TIMEOUT) ]; then \
			echo "sde_test_ch was not ready after $(READY_TIMEOUT)s. Probe output:"; \
			curl -sS -o /dev/null -w 'http %{http_code}\n' http://127.0.0.1:$(CH_PORT)/ping || true; \
			docker logs --tail 20 sde_test_ch; \
			exit 1; \
		fi; \
		sleep 1; \
	done

ch-down:
	-docker rm -f sde_test_ch

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf python/.mypy_cache python/.pytest_cache python/.ruff_cache
