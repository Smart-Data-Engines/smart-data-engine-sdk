# One order book, sent whole and as its changes

An exchange feed of one instrument is sent to the orderbook engine in both of a book stream's modes,
from Python and from TypeScript. The program then reads the book back by price and checks it
([`docs/orderbook.md`](../../docs/orderbook.md#books-by-price)). The feed is a bid and an ask side
of up to `--depth` prices around a walking mid price. Each step gives one side, either as a snapshot
of the whole side or as a few changes, where a quantity of 0 removes a price.

```bash
export BOOK_STREAM_OB_DSN=orderbook://host:port     # or orderbook://identity:SECRET@host:port?tls=on&ca=/path/ca.pem
python book_stream.py --steps 600 --depth 20
node book-stream.mjs  --steps 600 --depth 20        # with @smart-data-engines/sde installed
```

Each run sends the feed to two books of its own:
- **`mode="full"`** sends every update as the side whole, which the engine stores as a replacement.
- **`mode="incremental"`** sends a snapshot as its difference from the side last sent, or whole where
  that is smaller. It sends changes as the feed gave them. A side also goes whole at the first send and
  at every resync.

At every tenth step the program records the feed's own book. Afterwards it asks the engine for the
book at each of those instants with `book_at` / `bookAt`, for both books, and compares the two. The
program exits 1 on any difference. It prints what each stream sent:

```json
{
  "steps": 600,
  "books_read": 120,
  "mismatches": [],
  "streams": {
    "full": {"updates": 600, "sides_whole": 600, "updates_of_changes": 0, "unchanged": 0, "levels_sent": 8842},
    "incremental": {"updates": 600, "sides_whole": 116, "updates_of_changes": 484, "unchanged": 0, "levels_sent": 3532}
  }
}
```

That is a run against the engine at the SDK's pin, from either half. Both halves walk the same feed
from the same seed, so they send the same updates and print the same counts.
`python/tests/test_orderbook_book_stream_example.py` holds them to it in the `orderbook` CI job.

The placement map is written in the program, unsigned. The library reads a map without an account,
which is a documented mode. A map the control plane signs places `DepthLevel` on the orderbook
engine the same way. A book stream needs a server that announces `replace_side`, the engine's #233,
and refuses one that does not before sending anything.
