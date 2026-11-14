#!/bin/bash
# The orderbook slices of the three libraries against the engine's release, and the engine's released
# Python client against the pinned server. See README.md beside this file for what each run shows.
#
#   SDK=<this repository, with typescript/dist and cpp/build/check built>
#   OLD_TCP=host:port OLD_SECURE_DSN=orderbook://...   an ob_tcp_server built from the tag v0.1.0,
#                                                      plain, and with --auth-secret-file and --tls-client
#   PIN_TCP=host:port PIN_SECURE_DSN=orderbook://...   the same two at .github/orderbook-engine.txt
#   OUT=<directory for the logs>  WORK=<scratch directory>
#
# Every run is bounded and its exit status is written to OUT/status.txt; nothing here judges it.
set -u
: "${SDK:?}" "${OLD_TCP:?}" "${OLD_SECURE_DSN:?}" "${PIN_TCP:?}" "${PIN_SECURE_DSN:?}" "${OUT:?}" "${WORK:?}"
mkdir -p "$OUT" "$WORK"
: > "$OUT/status.txt"

# The environment a client gets from the index: this library with its extra, and nothing of the
# engine's checkout on the path.
python3 -m venv "$WORK/released"
"$WORK/released/bin/python" -m pip install -q --upgrade pip
"$WORK/released/bin/python" -m pip install -q "$SDK/python[orderbook,signed]" 'orderbook-dbengine==0.1.0' pytest
PY="$WORK/released/bin/python"
"$PY" -m pip list 2>/dev/null | grep -i -E '^(orderbook-dbengine|smart-data-engine-sdk|cryptography|pytest) ' > "$OUT/environment.txt"

run() {  # run <name> <directory> <command...>
  local name="$1" directory="$2"; shift 2
  ( cd "$directory" && timeout 900 "$@" ) > "$OUT/$name.log" 2>&1
  echo "$name=$?" >> "$OUT/status.txt"
}

export SDE_ORDERBOOK=1
unset PYTHONPATH OB_LIB_PATH

# 1. The release's server, the release's client.
SDE_ORDERBOOK_TCP="$OLD_TCP" SDE_ORDERBOOK_SECURE_DSN="$OLD_SECURE_DSN" \
  run python-released-client-against-0.1.0 "$SDK/python" "$PY" -m pytest -p no:cacheprovider -rfEs \
    tests/test_orderbook_slice.py tests/test_orderbook_session_slice.py tests/test_orderbook_books.py \
    -k "not local"

# A book as of a moment, asked of the release's server directly: the stream cases above stop at the
# stream, before any read.
SDE_ORDERBOOK_TCP="$OLD_TCP" run book-as-of-a-moment-against-0.1.0 "$SDK/python" "$PY" - <<'PY'
import os

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError

host, port = os.environ["SDE_ORDERBOOK_TCP"].rsplit(":", 1)
engine = OrderbookEngine(host=host, port=int(port))
engine.connect()
print("the server announces replace_side:", engine.supports_replace_side())
try:
    engine.book_at(sde.ORDERBOOK_TABLE, symbol="NOBODY", exchange="binance", at_ns=1)
except EngineError as refusal:
    print("book_at refused:", refusal)
else:
    raise SystemExit("book_at answered")
PY

# 2. and 3. TypeScript and C++ speak the engine's protocol themselves; the reference they read
# beside is the interpreter above.
SDE_ORDERBOOK_TCP="$OLD_TCP" SDE_ORDERBOOK_SECURE_DSN="$OLD_SECURE_DSN" SDE_PYTHON="$PY" \
  run typescript-against-0.1.0 "$SDK/typescript" npx vitest run \
    tests/orderbook.slice.test.ts tests/orderbook.books.test.ts
SDE_ORDERBOOK_TCP="$OLD_TCP" SDE_ORDERBOOK_SECURE_DSN="$OLD_SECURE_DSN" SDE_PYTHON="$PY" \
  run cpp-against-0.1.0 "$SDK" ctest --test-dir cpp/build/check --output-on-failure \
    -R '^(OrderbookSlice|OrderbookSecureSlice|BooksSlice)\.'

# 4. The release's client, a newer server: the step the `orderbook` CI job runs.
SDE_ORDERBOOK_TCP="$PIN_TCP" SDE_ORDERBOOK_SECURE_DSN="$PIN_SECURE_DSN" SDE_ORDERBOOK_RELEASED_CLIENT=1 \
  run python-released-client-against-the-pin "$SDK/python" "$PY" -m pytest -p no:cacheprovider -rfEs \
    tests/test_orderbook_released_client.py tests/test_orderbook_slice.py \
    tests/test_orderbook_session_slice.py -k "not local"

cat "$OUT/status.txt"
