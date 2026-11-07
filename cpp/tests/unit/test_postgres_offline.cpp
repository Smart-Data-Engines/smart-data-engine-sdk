/// The PostgreSQL adapter where no server is needed: what it refuses before it would use a
/// connection. A batch handed to the adapter directly, without a session, is checked as a session
/// checks it - the reference's adapter runs `sde.bulk.batch_columns` too - and a chunk to copy is
/// refused in the reference's words when its rows disagree.

#include <cstdlib>
#include <functional>
#include <utility>
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

// specs/secure-connections: a DSN reaching a host other than the loopback or a Unix socket needs a
// TLS policy, in the string or as PGSSLMODE, before anything connects.
namespace {

using Variables = std::vector<std::pair<const char*, const char*>>;

// Values are passed as a named Variables{...}: a bare braced list is ambiguous to GCC 12, which
// can also read two string literals as an iterator range for the vector.
class Environment {
 public:
  Environment() : Environment(Variables{}) {}
  explicit Environment(Variables values) {
    for (const char* name : {"PGSSLMODE", "PGHOST", "PGHOSTADDR"}) {
      if (const char* old = std::getenv(name)) saved_.emplace_back(name, old);
      ::unsetenv(name);
    }
    for (const auto& [name, value] : values) ::setenv(name, value, 1);
  }
  ~Environment() {
    for (const char* name : {"PGSSLMODE", "PGHOST", "PGHOSTADDR"}) ::unsetenv(name);
    for (const auto& [name, value] : saved_) ::setenv(name.c_str(), value.c_str(), 1);
  }

 private:
  std::vector<std::pair<std::string, std::string>> saved_;
};

TEST(PlaintextOnlyWhenSaid, PostgresRefusesARemoteHostWithNoTlsPolicy) {
  const Environment clean;
  for (const char* dsn : {"postgresql://app:secret@db.example.com:5432/sde",
                          "postgresql://app:secret@10.0.0.5/sde", "host=db.example.com dbname=sde",
                          "host=127.0.0.1,db.example.com dbname=sde", "hostaddr=10.0.0.5 dbname=sde"}) {
    const std::string refused = refusal_of([&] { sde::PostgresEngine engine(dsn); });
    EXPECT_NE(refused.find("sslmode=verify-full"), std::string::npos) << dsn << ": " << refused;
    EXPECT_EQ(refused.find("secret@"), std::string::npos) << refused;
  }
  for (const char* dsn : {"postgresql://app:secret@127.0.0.1:55432/sde",
                          "postgresql://app:secret@localhost/sde", "postgresql://app:secret@[::1]/sde",
                          "host=/var/run/postgresql dbname=sde", "dbname=sde",
                          "postgresql://app:secret@db.example.com/sde?sslmode=verify-full",
                          "postgresql://app:secret@db.example.com/sde?sslmode=disable",
                          "host=db.example.com dbname=sde sslmode=require"}) {
    EXPECT_EQ(refusal_of([&] { sde::PostgresEngine engine(dsn); }), "") << dsn;
  }
}

TEST(PlaintextOnlyWhenSaid, PostgresReadsTheEnvironmentAsLibpqDoes) {
  {
    const Environment remote(Variables{{"PGHOST", "db.example.com"}});
    EXPECT_NE(refusal_of([] { sde::PostgresEngine engine("dbname=sde"); }).find("TLS policy"),
              std::string::npos);
  }
  const Environment stated(Variables{{"PGHOST", "db.example.com"}, {"PGSSLMODE", "verify-full"}});
  EXPECT_EQ(refusal_of([] { sde::PostgresEngine engine("dbname=sde"); }), "");
}

}  // namespace
