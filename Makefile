# One entry point per language, plus the conformance suite that all of them have to pass.
#
# `make check` is what CI runs and what you should run before pushing. It deliberately does not stop
# at the first failure across languages: if Python and TypeScript both diverged from the contract you
# want to see both, because the fix is usually in the contract rather than in either library.

PY := python/.venv/bin

.PHONY: help check python-check python-test python-lint python-types conformance clean pg-up pg-down

help:
	@echo "check         everything CI runs"
	@echo "python-check  lint, types and tests for the Python library"
	@echo "conformance   run the shared vectors (currently: Python)"
	@echo "pg-up         start a PostgreSQL for the integration slice"
	@echo "pg-down       stop it"

check: python-check

python-check: python-lint python-types python-test

python-lint:
	cd python && .venv/bin/ruff check src tests

python-types:
	cd python && .venv/bin/mypy src

python-test:
	cd python && .venv/bin/python -m pytest

conformance:
	cd python && .venv/bin/python -m pytest tests/test_conformance.py -v

# The integration slice runs against a real PostgreSQL rather than a fake, because a fake would agree
# with whatever this library believes about types, quoting and transactions - which is exactly the set
# of beliefs worth checking.
pg-up:
	docker run -d --name sde_test_pg -e POSTGRES_PASSWORD=sde -e POSTGRES_DB=sde \
		-p 127.0.0.1:55432:5432 postgres:15-alpine
	@echo "SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde"

pg-down:
	docker rm -f sde_test_pg

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf python/.mypy_cache python/.pytest_cache python/.ruff_cache
