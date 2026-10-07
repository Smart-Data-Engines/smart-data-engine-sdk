#pragma once

/// How an adapter tells a session that the catalogue refused its login a size, rather than that the
/// engine failed. The reference reads the difference from its driver's error beneath its own -
/// PostgreSQL's SQLSTATE 42501, ClickHouse's access error 497 - and here the adapter, which holds
/// that error, throws this instead of a plain `EngineError`, in the same words. The words of a
/// message decide nothing: they are the server's, and a server can say anything.

#include "sde/errors.hpp"

namespace sde::detail {

class CatalogueRefused : public EngineError {
 public:
  using EngineError::EngineError;
  ~CatalogueRefused() override;
};

}  // namespace sde::detail
