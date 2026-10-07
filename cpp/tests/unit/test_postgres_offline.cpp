/// The PostgreSQL adapter where no server is needed: what it refuses before it would use a
/// connection. A batch handed to the adapter directly, without a session, is checked as a session
/// checks it - the reference's adapter runs `sde.bulk.batch_columns` too - and a chunk to copy is
/// refused in the reference's words when its rows disagree.

#include <functional>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/postgres.hpp"
#include "sde/value.hpp"

namespace {

/// Never connected: every refusal below comes before a connection would be used.
sde::PostgresEngine unconnected() { return sde::PostgresEngine("postgresql://nobody@127.0.0.1:1/x"); }

std::string refusal_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const sde::SdeError& error) {
    return error.what();
  }
  return "";
}

TEST(PostgresOffline, ABatchIsCheckedAsTheSessionChecksItBeforeAnyConnection) {
  sde::PostgresEngine engine = unconnected();
  const std::vector<sde::Row> mixed = {{{"a b", std::int64_t{1}}, {"c", std::int64_t{2}}},
                                       {{"a", std::int64_t{1}}, {"b c", std::int64_t{2}}}};
  EXPECT_THROW(engine.insert_many("event", mixed), sde::BulkWriteRefused);
  EXPECT_EQ(refusal_of([&] { engine.insert_many("event", mixed); }),
            "all batch rows must have the same fields");
  EXPECT_EQ(refusal_of([&] { engine.insert_many("event", {sde::Row{}}); }),
            "each batch row must be a nonempty mapping with string fields");
  const std::vector<sde::Row> too_many(1001, sde::Row{{"id", std::int64_t{1}}});
  EXPECT_EQ(refusal_of([&] { engine.insert_many("event", too_many); }),
            "a batch may contain at most 1000 rows");
  // Nothing to write is nothing to do, and a sound batch gets as far as the connection.
  EXPECT_NO_THROW(engine.insert_many("event", {}));
  EXPECT_EQ(refusal_of([&] { engine.insert_many("event", {{{"id", std::int64_t{1}}}}); }),
            "not connected; call connect() first");
}

TEST(PostgresOffline, AChunkToCopyWhoseRowsDisagreeIsRefusedInTheReferencesWords) {
  sde::PostgresEngine engine = unconnected();
  EXPECT_EQ(refusal_of([&] {
              engine.copy_in("event", {{{"a b", std::int64_t{1}}, {"c", std::int64_t{2}}},
                                       {{"a", std::int64_t{1}}, {"b c", std::int64_t{2}}}});
            }),
            "copy_in into event was given rows with different columns (['a b', 'c'] and ['a', "
            "'b c']). A chunk comes from one table, so this is a caller assembling it from two.");
}

}  // namespace
