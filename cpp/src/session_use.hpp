#pragma once

/// The guard every operation of a session takes, shared with the free functions that act through a
/// session (`backfill`, `verify`).

#include <thread>

#include "sde/errors.hpp"
#include "sde/session.hpp"

namespace sde {

/// One thread in a session at a time. The owner may re-enter - a transaction's body calls the session
/// - and anybody else is refused before any engine is called, rather than racing it.
class Session::Use {
 public:
  explicit Use(const Session& session) : session_(const_cast<Session&>(session)) {
    const std::thread::id me = std::this_thread::get_id();
    if (session_.owner_.load() == me) {
      ++session_.depth_;
      return;
    }
    std::thread::id nobody{};
    if (!session_.owner_.compare_exchange_strong(nobody, me)) {
      throw ResourceBusy(
          "this session is in use by another thread. A session is one unit of work, like a "
          "database connection; open one per thread, over the same engines if their adapters "
          "allow it.");
    }
    session_.depth_ = 1;
  }
  ~Use() {
    if (--session_.depth_ == 0) session_.owner_.store(std::thread::id{});
  }
  Use(const Use&) = delete;
  Use& operator=(const Use&) = delete;

 private:
  Session& session_;
};

}  // namespace sde
