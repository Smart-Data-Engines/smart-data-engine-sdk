/// Layouts derived from a model. The table names are the part that must agree across languages:
/// `snake_case` and the lowercase under it were compared with Python's and JavaScript's on every
/// scalar code point and on a hundred thousand generated names, and agreed on all of them. The
/// cases below are the ones worth naming.

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/unicode.hpp"

namespace {

TEST(ToLower, FullCaseMappingAsPythonAndJavaScript) {
  EXPECT_EQ(sde::to_lower("ORDER"), "order");
  EXPECT_EQ(sde::to_lower("\xc4\xb0stanbul"), "i\xcc\x87stanbul");  // U+0130 keeps its dot
  EXPECT_EQ(sde::to_lower("\xe1\xba\x9e"), "\xc3\x9f");               // capital sharp s
  EXPECT_EQ(sde::to_lower("\xd0\x96"), "\xd0\xb6");                   // Cyrillic
  EXPECT_EQ(sde::to_lower("\xf0\x90\x90\x80"), "\xf0\x90\x90\xa8");   // Deseret, astral
}

TEST(ToLower, CapitalSigmaEndingAWordIsFinal) {
  // ΣΟΦΙΑΣ -> σοφιας: the last sigma ends a word, the first begins one.
  EXPECT_EQ(sde::to_lower("\xce\xa3\xce\x9f\xce\xa6\xce\x99\xce\x91\xce\xa3"),
            "\xcf\x83\xce\xbf\xcf\x86\xce\xb9\xce\xb1\xcf\x82");
  // ΣΑΣ Σ -> σας σ: a sigma alone has no cased letter before it.
  EXPECT_EQ(sde::to_lower("\xce\xa3\xce\x91\xce\xa3 \xce\xa3"),
            "\xcf\x83\xce\xb1\xcf\x82 \xcf\x83");
  // Case-ignorable characters are skipped on both sides: A'Σ is final, AΣ'B is not.
  EXPECT_EQ(sde::to_lower("A'\xce\xa3"), "a'\xcf\x82");
  EXPECT_EQ(sde::to_lower("A\xce\xa3'B"), "a\xcf\x83'b");
  // A roman numeral is cased without being a letter (Other_Uppercase).
  EXPECT_EQ(sde::to_lower("\xe2\x85\xa0\xce\xa3"), "\xe2\x85\xb0\xcf\x82");
}

TEST(ToLower, RefusesBytesThatAreNotText) {
  EXPECT_THROW((void)sde::to_lower("\xed\xa0\x80"), sde::CanonicalError);
  EXPECT_THROW((void)sde::to_lower("\xff"), sde::CanonicalError);
}

TEST(SnakeCase, AsTheReferenceDerivesTableNames) {
  EXPECT_EQ(sde::snake_case("OrderLine"), "order_line");
  EXPECT_EQ(sde::snake_case("HTTPRequest"), "http_request");
  EXPECT_EQ(sde::snake_case("getHTTPResponseCode"), "get_http_response_code");
  EXPECT_EQ(sde::snake_case("A1B"), "a1_b");
  EXPECT_EQ(sde::snake_case("IOStream"), "io_stream");
  EXPECT_EQ(sde::snake_case("ABC"), "abc");
  EXPECT_EQ(sde::snake_case("aB"), "a_b");
  EXPECT_EQ(sde::snake_case(""), "");
  EXPECT_EQ(sde::snake_case("Zamówienie"), "zamówienie");
  EXPECT_EQ(sde::snake_case("ZamówienieKlienta"), "zamówienie_klienta");
  // A boundary is ASCII only: Ż is not [A-Z], so no underscore before it.
  EXPECT_EQ(sde::snake_case("\xc5\xbb\xc3\xb3\xc5\x82wPRO"), "\xc5\xbc\xc3\xb3\xc5\x82w_pro");
  EXPECT_EQ(sde::snake_case("\xc4\xb0stanbul"), "i\xcc\x87stanbul");
}

TEST(SnakeCase, NormalisesFirst) {
  // E + U+0301 and the composed É are one entity name, so they must be one table.
  EXPECT_EQ(sde::snake_case("E\xcc\x81vent"), sde::snake_case("\xc3\x89vent"));
  EXPECT_EQ(sde::snake_case("\xc3\x89vent"), "\xc3\xa9vent");
}

sde::Model shop() {
  return sde::load_neutral_model(R"json({
    "entities": [
      {"name": "Customer", "fields": [{"name": "id", "type": "uuid"},
                                      {"name": "email", "type": "string", "nullable": true}],
       "key": ["id"]},
      {"name": "OrderLine", "fields": [{"name": "order_id", "type": "int64"},
                                       {"name": "line", "type": "int32"},
                                       {"name": "price", "type": "decimal(12,2)"},
                                       {"name": "note", "type": "string", "nullable": true}],
       "key": ["order_id", "line"]},
      {"name": "PurchaseOrder", "fields": [{"name": "id", "type": "int64"},
                                           {"name": "placed", "type": "timestamptz"}],
       "key": ["id"]}
    ],
    "relations": [
      {"name": "buyer", "from": "PurchaseOrder", "to": "Customer"},
      {"name": "order", "from": "OrderLine", "to": "PurchaseOrder"}
    ]
  })json");
}

TEST(GroupColumns, FieldsThenOneColumnPerRelationKeyField) {
  const sde::Model model = shop();
  ASSERT_EQ(model.groups().size(), 1U);
  const auto columns = sde::group_columns(model, model.groups().front());
  const sde::NeutralColumns want_order = {{"id", "int64"}, {"placed", "timestamptz"},
                                          {"buyer_id", "uuid"}};
  EXPECT_EQ(columns.at("PurchaseOrder"), want_order);
  // `order_id` is a declared field and also the relation's column: one column, in its place.
  const sde::NeutralColumns want_line = {{"line", "int32"},
                                         {"note", "string"},
                                         {"order_id", "int64"},
                                         {"price", "decimal(12,2)"}};
  EXPECT_EQ(columns.at("OrderLine"), want_line);
  EXPECT_EQ(sde::stored_types(model, model.groups().front()),
            (std::vector<std::string>{"decimal(12,2)", "int32", "int64", "string", "timestamptz",
                                      "uuid"}));
}

TEST(DefaultLayout, PostgresHasTablesColumnsAndRelationIndexes) {
  const sde::Model model = shop();
  const sde::PhysicalLayout layout = sde::default_layout(model, model.groups().front());
  EXPECT_EQ(layout.tables.at("OrderLine"), "order_line");
  EXPECT_EQ(layout.tables.at("PurchaseOrder"), "purchase_order");
  EXPECT_EQ(layout.columns.at("OrderLine").at("price"), "numeric(12,2)");
  EXPECT_EQ(layout.columns.at("Customer").at("email"), "text");
  EXPECT_EQ(layout.columns.at("PurchaseOrder").at("placed"), "timestamptz");
  ASSERT_EQ(layout.indexes.size(), 2U);
  EXPECT_EQ(layout.indexes[0].entity, "OrderLine");
  EXPECT_EQ(layout.indexes[0].name, "order_line_order_idx");
  EXPECT_EQ(layout.indexes[0].columns, (std::vector<std::string>{"order_id"}));
  EXPECT_EQ(layout.indexes[0].method, "btree");
  EXPECT_FALSE(layout.indexes[0].method_written);
  EXPECT_EQ(layout.indexes[1].name, "purchase_order_buyer_idx");
  EXPECT_TRUE(layout.key_order.empty());
  EXPECT_TRUE(layout.partition_by.empty());
}

TEST(DefaultLayout, ClickHouseKeepsNullabilityAndHasNoIndexRows) {
  const sde::Model model = shop();
  const sde::PhysicalLayout layout = sde::default_layout(model, model.groups().front(), "clickhouse");
  EXPECT_EQ(layout.columns.at("Customer").at("email"), "Nullable(String)");
  EXPECT_EQ(layout.columns.at("Customer").at("id"), "UUID");
  EXPECT_EQ(layout.columns.at("OrderLine").at("price"), "Decimal(12, 2)");
  EXPECT_EQ(layout.columns.at("PurchaseOrder").at("placed"), "DateTime64(6, 'UTC')");
  EXPECT_TRUE(layout.indexes.empty());
}

TEST(DefaultLayout, AnUnknownDialectIsRefusedByName) {
  const sde::Model model = shop();
  try {
    (void)sde::default_layout(model, model.groups().front(), "mysql");
    FAIL() << "a dialect nobody wrote a layout for was accepted";
  } catch (const sde::DeclarationError& error) {
    EXPECT_NE(std::string(error.what()).find("no default layout for dialect 'mysql'"),
              std::string::npos)
        << error.what();
    EXPECT_NE(std::string(error.what()).find("['clickhouse', 'orderbook', 'postgres']"),
              std::string::npos);
  }
}

sde::Model book(const char* fields) {
  return sde::load_neutral_model(std::string(R"({"entities": [{"name": "Book", "fields": [)") +
                                 fields + R"(], "key": ["symbol"]}]})");
}

constexpr const char* kShape =
    R"({"name": "symbol", "type": "string"}, {"name": "exchange", "type": "string"},
       {"name": "timestamp_ns", "type": "int64"}, {"name": "side", "type": "string"},
       {"name": "level", "type": "int32"}, {"name": "price", "type": "int64"},
       {"name": "quantity", "type": "int64"}, {"name": "order_count", "type": "int32"},
       {"name": "sequence_number", "type": "int64", "nullable": true})";

TEST(Orderbook, TheFixedShapeIsTheEnginesOwn) {
  const sde::Model model = book(kShape);
  const auto& group = model.groups().front();
  EXPECT_FALSE(sde::fixed_schema_mismatch(sde::group_columns(model, group), "orderbook",
                                          sde::group_nullable(model, group)));
  const sde::PhysicalLayout layout = sde::default_layout(model, group, "orderbook");
  EXPECT_EQ(layout.tables.at("Book"), "orderbook");
  EXPECT_EQ(layout.columns.at("Book").at("symbol"), "char*");
  EXPECT_EQ(layout.columns.at("Book").at("level"), "uint32_t");
  EXPECT_EQ(layout.columns.at("Book").at("price"), "int64_t");
  EXPECT_TRUE(sde::schema_is_fixed("orderbook"));
  EXPECT_FALSE(sde::schema_is_fixed("postgres"));
  // Not the business of any other dialect.
  EXPECT_FALSE(sde::fixed_schema_mismatch(sde::group_columns(model, group), "postgres", {}));
}

TEST(Orderbook, EveryWayAModelMissesTheShapeIsNamed) {
  const sde::Model model = book(
      R"({"name": "symbol", "type": "string"}, {"name": "exchange", "type": "string"},
         {"name": "timestamp_ns", "type": "int64"}, {"name": "side", "type": "string"},
         {"name": "level", "type": "int64"}, {"name": "price", "type": "int64"},
         {"name": "quantity", "type": "int64", "nullable": true},
         {"name": "order_count", "type": "int32"}, {"name": "sequence_number", "type": "int64"},
         {"name": "venue", "type": "string"})");
  const auto& group = model.groups().front();
  const auto why = sde::fixed_schema_mismatch(sde::group_columns(model, group), "orderbook",
                                              sde::group_nullable(model, group));
  ASSERT_TRUE(why.has_value());
  EXPECT_EQ(*why,
            "Book declares ['venue'], which this engine has nowhere to put. level is declared "
            "'int64' and this engine stores 'int32'. Book declares ['quantity'] nullable, and this "
            "engine stores no null there. Book declares ['sequence_number'] required, and this "
            "engine assigns it: a row is written without it and read back with the engine's "
            "value. This engine's schema is fixed in the engine and not chosen by us, so a model "
            "either is that shape or cannot be stored here. The shape is exactly: symbol: string, "
            "exchange: string, timestamp_ns: int64, side: string, level: int32, price: int64, "
            "quantity: int64, order_count: int32, sequence_number: int64, nullable.");
  EXPECT_THROW((void)sde::default_layout(model, group, "orderbook"), sde::DeclarationError);
}

TEST(Orderbook, AGroupOfTwoHasNoRoom) {
  const sde::Model model = shop();
  const auto why = sde::fixed_schema_mismatch(sde::group_columns(model, model.groups().front()),
                                              "orderbook", {});
  ASSERT_TRUE(why.has_value());
  EXPECT_EQ(why->rfind("this engine stores one thing, and this group has 3 entities "
                       "(['Customer', 'OrderLine', 'PurchaseOrder'])",
                       0),
            0U)
      << *why;
}

TEST(ColumnTypes, OneTypePerDialectAndNoDefaulting) {
  EXPECT_EQ(sde::column_type("timestamp", "clickhouse"), "DateTime64(6)");
  EXPECT_EQ(sde::column_type("json", "postgres"), "jsonb");
  EXPECT_THROW((void)sde::column_type("bytes", "clickhouse"), sde::DeclarationError);
  EXPECT_THROW((void)sde::column_type("decimal(12,2)", "orderbook"), sde::DeclarationError);
  EXPECT_THROW((void)sde::column_type("uuid", "orderbook"), sde::DeclarationError);
  EXPECT_THROW((void)sde::column_type("int64", "oracle"), sde::DeclarationError);
}

TEST(CanStore, AnswersAboutEnginesAndRefusesTheRest) {
  EXPECT_TRUE(sde::can_store("bytes", "postgres"));
  EXPECT_FALSE(sde::can_store("bytes", "clickhouse"));
  EXPECT_FALSE(sde::can_store("json", "clickhouse"));
  EXPECT_TRUE(sde::can_store("decimal(38,10)", "clickhouse"));
  EXPECT_FALSE(sde::can_store("decimal(12,2)", "orderbook"));
  // "Cannot store" is a claim about an engine; these are claims about something else.
  EXPECT_THROW((void)sde::can_store("int64", "oracle"), sde::DeclarationError);
  EXPECT_THROW((void)sde::can_store("decimal(x)", "postgres"), sde::DeclarationError);
}

}  // namespace
