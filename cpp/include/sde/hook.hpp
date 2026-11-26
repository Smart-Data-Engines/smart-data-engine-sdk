#pragma once

/// What a session tells the application about each operation, when the application asks.
///
/// A session given a hook (`SessionOptions::hook`) calls it once for every operation it completes -
/// with what the operation was, where it went, how long it took, how many rows it moved and how it
/// ended - and with nothing the operation carried: no key, no row, no filter, no statement, no
/// connection string and no error message, since a driver's message can quote a key
/// (docs/observability.md). The hook runs after the operation, on the thread that ran it; a hook
/// that throws changes nothing about the operation's result.
///
/// The library depends on no tracing or metrics library. Turning a record into a span or a
/// histogram is a few lines of the application's own code, shown in docs/observability.md.

#include <cstdint>
#include <exception>
#include <functional>
#include <string>
#include <string_view>

namespace sde {

/// The methods a record can name, in the contract's spelling; `transaction` is a whole
/// transaction, from open to its end.
inline constexpr std::string_view OPERATIONS[] = {
    "book_at", "book_stream", "count", "get",       "save",
    "save_many", "scan",      "search", "summarize", "transaction",
};

/// One completed operation. Fields in the contract's order (`observability/001`).
struct OperationRecord {
  std::string operation;        ///< the session method: one of `OPERATIONS`
  std::string entity;           ///< in the application's own names even when the model is hashed
  std::string group;            ///< the colocation group, also in the application's names
  std::string shape;            ///< the operation shape's id; empty for a transaction
  std::string engine;           ///< the engine's name in the placement map, never its address
  std::string dialect;          ///< postgres, clickhouse, elasticsearch or orderbook
  std::string materialization;  ///< the id of the materialization that served the operation
  std::int64_t duration_ns = 0;  ///< on the monotonic clock; a span starts at its end minus this
  std::uint64_t rows = 0;       ///< written or returned; 1 for a count or a summary
  std::string outcome;          ///< `ok`, a class of the library's hierarchy, or `unexpected`
};

using OperationHook = std::function<void(const OperationRecord&)>;

/// The record's fields, in order.
inline constexpr std::string_view OPERATION_FIELDS[] = {
    "operation", "entity",     "group", "shape", "engine", "dialect", "materialization",
    "duration_ns", "rows", "outcome",
};

/// `ok` for no error, the class of an error of the library's hierarchy (`EngineError`,
/// `ResourceBusy`...), or `unexpected` for anything else: a class name is all that leaves, because
/// a message can hold a value.
[[nodiscard]] std::string outcome_of(const std::exception_ptr& error);

}  // namespace sde
