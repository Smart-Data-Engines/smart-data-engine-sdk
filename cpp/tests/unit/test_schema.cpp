/// The DDL renderer's rules the `schema/` vectors do not reach one by one: quoting, the refusals of
/// a layout built by hand, and the reasons a view cannot exist.

#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/schema.hpp"

namespace {

sde::Index index(std::string entity, std::string name, std::vector<std::string> columns,
                 std::string method = "btree") {
  sde::Index out;
  out.entity = std::move(entity);
  out.name = std::move(name);
  out.columns = std::move(columns);
  out.method = std::move(method);
  return out;
}

std::string refusal(const std::function<void()>& body) {
  try {
    body();
  } catch (const sde::EngineError& refused) {
    return refused.what();
  }
  return "";
}

TEST(Quote, EachDialectsRule) {
  EXPECT_EQ(sde::quote_ansi("plain"), "\"plain\"");
  EXPECT_EQ(sde::quote_ansi("say \"hi\""), "\"say \"\"hi\"\"\"");
  // A backslash is literal inside PostgreSQL's quotes.
  EXPECT_EQ(sde::quote_ansi("a\\nb"), "\"a\\nb\"");
  EXPECT_EQ(sde::quote_backtick("plain"), "`plain`");
  // One pass, so neither escape can eat the other: `a\`b` must not become `a\\`b`.
  EXPECT_EQ(sde::quote_backtick("a\\`b"), "`a\\\\\\`b`");
  EXPECT_EQ(sde::quote_backtick("a\\nb"), "`a\\\\nb`");
  EXPECT_EQ(sde::quote_identifier("postgres", "t"), "\"t\"");
  EXPECT_EQ(sde::quote_identifier("clickhouse", "t"), "`t`");
  EXPECT_NE(refusal([] { (void)sde::quote_identifier("orderbook", "t"); }).find("no identifier quoting"),
            std::string::npos);
  EXPECT_NE(refusal([] { (void)sde::quote_identifier("mysql", "t"); }).find("'mysql'"), std::string::npos);
}

sde::PhysicalLayout one_table() {
  sde::PhysicalLayout layout;
  layout.tables = {{"Reading", "reading"}};
  layout.columns = {{"Reading", {{"id", "bigint"}, {"station", "text"}, {"at", "timestamptz"}}}};
  return layout;
}

const std::map<std::string, std::vector<std::string>> kKeys = {{"Reading", {"id"}}};

TEST(SchemaStatements, TextColumnsAndTheirIndexesTakeTheReadsCollation) {
  sde::PhysicalLayout layout = one_table();
  layout.indexes.push_back(index("Reading", "reading_station_idx", {"station", "at"}));
  layout.indexes.push_back(index("Reading", "reading_at_brin", {"at"}, "brin"));
  EXPECT_EQ(sde::schema_statements(layout, kKeys, "postgres"),
            (std::vector<std::string>{
                "CREATE TABLE IF NOT EXISTS \"reading\" (\"at\" timestamptz, \"id\" bigint, "
                "\"station\" text COLLATE \"C\", PRIMARY KEY (\"id\"))",
                "CREATE INDEX IF NOT EXISTS \"reading_at_brin\" ON \"reading\" USING brin (\"at\")",
                "CREATE INDEX IF NOT EXISTS \"reading_station_idx\" ON \"reading\" "
                "(\"station\" COLLATE \"C\", \"at\")"}));
}

TEST(SchemaStatements, RefusesALayoutBuiltByHand) {
  sde::PhysicalLayout layout = one_table();
  EXPECT_EQ(refusal([&] { (void)sde::schema_statements(layout, {}, "postgres"); }),
            "no key for 'Reading'; a table without one cannot be addressed");
  sde::PhysicalLayout bare = layout;
  bare.columns.clear();
  EXPECT_EQ(refusal([&] { (void)sde::schema_statements(bare, kKeys, "postgres"); }),
            "the layout gives no columns for 'Reading'");
  sde::PhysicalLayout reordered = layout;
  reordered.key_order = {{"Reading", {"station"}}};
  EXPECT_EQ(refusal([&] { (void)sde::schema_statements(reordered, kKeys, "postgres"); }),
            "table 'reading': key_order['Reading'] is ['station'] and the key is ['id']. A key order "
            "must be a permutation of the key.");
  sde::PhysicalLayout partitioned = layout;
  partitioned.partition_by = {{"Reading", sde::Partition{"at", "day"}}};
  EXPECT_NE(refusal([&] { (void)sde::schema_statements(partitioned, kKeys, "postgres"); })
                .find("the layout partitions ['Reading'] and PostgreSQL partitioning is not rendered"),
            std::string::npos);
  // In ClickHouse the partition must be on a key column: the reference's own words.
  EXPECT_EQ(refusal([&] { (void)sde::schema_statements(partitioned, kKeys, "clickhouse"); }),
            "table 'reading': partition_by['Reading'] partitions on 'at', outside the key ['id']; "
            "duplicates of one key would survive merges in two partitions.");
}

TEST(SchemaStatements, AFixedSchemaEngineTakesNoDesign) {
  sde::PhysicalLayout layout;
  layout.tables = {{"Book", "orderbook"}};
  EXPECT_TRUE(sde::schema_statements(layout, {}, "orderbook").empty());
  layout.key_order = {{"Book", {"symbol"}}};
  EXPECT_NE(refusal([&] { (void)sde::schema_statements(layout, {}, "orderbook"); })
                .find("this engine's schema is fixed in its own source"),
            std::string::npos);
}

TEST(IndexClauses, EachDialectRefusesTheOthersMethods) {
  sde::Index minmax = index("Reading", "reading_at_minmax", {"at"}, "minmax");
  minmax.granularity = 4;
  EXPECT_EQ(sde::clickhouse_index_clause(minmax), "INDEX `reading_at_minmax` `at` TYPE minmax GRANULARITY 4");
  sde::Index set = minmax;
  set.method = "set";
  set.max_rows = 100;
  EXPECT_EQ(sde::clickhouse_index_clause(set), "INDEX `reading_at_minmax` `at` TYPE set(100) GRANULARITY 4");
  EXPECT_EQ(refusal([&] { (void)sde::postgres_index_target(minmax, "reading", {}); }),
            "index 'reading_at_minmax' is a minmax index, which is a ClickHouse data-skipping index; "
            "PostgreSQL has ['brin', 'btree']. This map was designed for another dialect.");
  const sde::Index btree = index("Reading", "reading_idx", {"at"});
  EXPECT_EQ(refusal([&] { (void)sde::clickhouse_index_clause(btree); }),
            "index 'reading_idx' is not a ClickHouse data-skipping index; this engine has "
            "['bloom_filter', 'minmax', 'set']");
  sde::Index two = minmax;
  two.columns = {"at", "id"};
  EXPECT_EQ(refusal([&] { (void)sde::clickhouse_index_clause(two); }),
            "the data-skipping index 'reading_at_minmax' must summarise exactly one column");
  sde::Index bare = minmax;
  bare.granularity.reset();
  EXPECT_NE(refusal([&] { (void)sde::clickhouse_index_clause(bare); }).find("has no granularity"),
            std::string::npos);
}

TEST(CompatibilityViews, SaysWhyAViewCannotExist) {
  sde::PhysicalLayout layout;
  layout.tables = {{"Book", "orderbook"}, {"Tick", "orderbook_ticks"}};
  const sde::CompatibilityViews fixed =
      sde::compatibility_views(layout, {{"Book", "book"}}, "orderbook");
  EXPECT_FALSE(fixed.complete());
  ASSERT_EQ(fixed.not_possible.size(), 2U);
  EXPECT_NE(fixed.not_possible[0].second.find("Its table is 'orderbook' and the old name was 'book'"),
            std::string::npos);
  // No old name is Python's None, as the reference writes it.
  EXPECT_NE(fixed.not_possible[1].second.find("the old name was None"), std::string::npos);
  EXPECT_EQ(refusal([&] { (void)sde::compatibility_views(layout, {}, "mysql"); }),
            "no compatibility view for dialect 'mysql'; this library renders ['clickhouse', "
            "'orderbook', 'postgres'].");
  sde::PhysicalLayout moved = one_table();
  moved.columns.clear();
  EXPECT_EQ(refusal([&] { (void)sde::compatibility_views(moved, {{"Reading", "readings"}}, "postgres"); }),
            "the layout gives no columns for 'Reading'");
}

TEST(SchemaIsFixed, RefusesADialectItHasNeverHeardOf) {
  EXPECT_EQ(refusal([] { (void)sde::schema_is_fixed(""); }),
            "unknown dialect ''; this library renders ['clickhouse', 'orderbook', 'postgres']. "
            "Answering False would say 'that engine takes DDL from us' about an engine it has "
            "never heard of.");
}

}  // namespace
