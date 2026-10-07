// Section 8b, point 4: a value its field's type does not hold is refused before any engine.
//
// The shared vectors pin the refusals and the form a value reaches an engine in (`errors/113` to
// `120`, `migration/200`); these pin each type's boundaries, as `test_admission.py` and
// `admission.test.ts` do in the other two libraries.

#include <cmath>
#include <cstdint>
#include <functional>
#include <limits>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "admission.hpp"
#include "sde/errors.hpp"
#include "sde/hashing.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/testing/memory.hpp"

namespace {

using sde::Value;

Value admits(const std::string& kind, const Value& value) {
  const sde::detail::Check check = sde::detail::check_for(kind);
  if (!check) throw std::logic_error("no check for " + kind);
  return check(value);
}

std::string refusal(const std::string& kind, const Value& value) {
  try {
    (void)admits(kind, value);
  } catch (const sde::detail::Misfit& misfit) {
    return misfit.what();
  }
  return "admitted";
}

std::string text_of(const Value& value) { return sde::dump_json(sde::testing::vector_form(value)); }

// --- decimal ------------------------------------------------------------------------------------

TEST(Admission, ADecimalThatFitsReachesTheEngineAtItsScale) {
  const std::vector<std::pair<Value, std::string>> cases{
      {sde::Decimal("1.23"), "1.23"},     {std::string("1.230"), "1.23"},
      {std::string("123E-2"), "1.23"},    {std::string("1E+3"), "1000.00"},
      {std::string("-0.00"), "0.00"},     {std::string("0E+100"), "0.00"},
      {std::string(" .5 "), "0.50"},      {std::string("+1.5"), "1.50"},
      {std::string("9999999999.99"), "9999999999.99"},
      {std::string("-9999999999.99"), "-9999999999.99"},
      {sde::Decimal("1.230000000000000000000000000000000"), "1.23"},
      {sde::Decimal("-0.00"), "0.00"},
  };
  for (const auto& [given, held] : cases) {
    const Value got = admits("decimal(12,2)", given);
    ASSERT_TRUE(std::holds_alternative<sde::Decimal>(got)) << held;
    EXPECT_EQ(std::get<sde::Decimal>(got).to_string(), held);
  }
}

TEST(Admission, AnIntegerGivenToADecimalStaysTheInteger) {
  EXPECT_EQ(admits("decimal(12,2)", std::int64_t{7}), Value(std::int64_t{7}));
  EXPECT_EQ(admits("decimal(12,2)", std::int64_t{-9'999'999'999}), Value(std::int64_t{-9'999'999'999}));
  EXPECT_EQ(refusal("decimal(12,2)", std::int64_t{10'000'000'000}), "more than 10 integer digits");
  // The most negative integer has no positive counterpart; its digits are still counted.
  EXPECT_EQ(refusal("decimal(12,2)", std::numeric_limits<std::int64_t>::min()),
            "more than 10 integer digits");
}

TEST(Admission, ADecimalThatDoesNotFitIsRefusedByClass) {
  const std::vector<std::pair<Value, std::string>> cases{
      {std::string("1.239"), "more than 2 fractional digits"},
      {std::string("0.001"), "more than 2 fractional digits"},
      {std::string("9999999999.995"), "more than 2 fractional digits"},
      // Both misfit: the fractional digits are named first, in every library.
      {std::string("12345678901.239"), "more than 2 fractional digits"},
      {std::string("12345678901.23"), "more than 10 integer digits"},
      {sde::Decimal("12345678901.23"), "more than 10 integer digits"},
      {std::string("1E+10"), "more than 10 integer digits"},
      {std::string("1e999999999999999999"), "more than 10 integer digits"},
      {std::string("1e-1999999999999999997"), "more than 2 fractional digits"},
      // Past the reference's own limits its Decimal() calls the text invalid.
      {std::string("1e1000000000000000000"), "a value that is not an exact decimal"},
      {std::string("1e-1999999999999999998"), "a value that is not an exact decimal"},
      {std::string("NaN"), "a value that is not an exact decimal"},
      {std::string("Infinity"), "a value that is not an exact decimal"},
      {1.5, "a value that is not an exact decimal"},
      {0.1 + 0.2, "a value that is not an exact decimal"},
      {true, "a value that is not an exact decimal"},
      {std::string("1_000.5"), "a value that is not an exact decimal"},
      {std::string(""), "a value that is not an exact decimal"},
  };
  for (const auto& [given, why] : cases) {
    EXPECT_EQ(refusal("decimal(12,2)", given), why) << sde::dump_json(sde::testing::value_to_json(given));
  }
}

TEST(Admission, TheEdgesOfADecimalWithNoIntegerOrNoFractionalDigits) {
  EXPECT_EQ(text_of(admits("decimal(2,2)", std::string("0.99"))), "\"0.99\"");
  EXPECT_EQ(refusal("decimal(2,2)", std::string("1.5")), "more than 0 integer digits");
  EXPECT_EQ(text_of(admits("decimal(5,0)", std::string("12345"))), "\"12345\"");
  EXPECT_EQ(refusal("decimal(5,0)", std::string("1.5")), "more than 0 fractional digits");
  EXPECT_EQ(refusal("decimal(5,0)", std::string("123456")), "more than 5 integer digits");
}

// --- integers -----------------------------------------------------------------------------------

TEST(Admission, IntegersInsideTheirTypeAreAdmittedAsGiven) {
  for (const std::int64_t edge : {std::int64_t{std::numeric_limits<std::int32_t>::min()},
                                  std::int64_t{std::numeric_limits<std::int32_t>::max()}}) {
    EXPECT_EQ(admits("int32", edge), Value(edge));
  }
  for (const std::int64_t edge :
       {std::numeric_limits<std::int64_t>::min(), std::numeric_limits<std::int64_t>::max()}) {
    EXPECT_EQ(admits("int64", edge), Value(edge));
  }
}

TEST(Admission, AnIntegerItsTypeDoesNotHoldIsRefused) {
  EXPECT_EQ(refusal("int32", std::int64_t{2'147'483'648}), "an integer outside int32");
  EXPECT_EQ(refusal("int32", std::int64_t{-2'147'483'649}), "an integer outside int32");
  EXPECT_EQ(refusal("int32", true), "a value that is not an integer");
  EXPECT_EQ(refusal("int64", false), "a value that is not an integer");
  EXPECT_EQ(refusal("int32", 1.0), "a value that is not an integer");
  EXPECT_EQ(refusal("int32", std::string("7")), "a value that is not an integer");
  EXPECT_EQ(refusal("int64", sde::Decimal("7")), "a value that is not an integer");
}

// --- floats -------------------------------------------------------------------------------------

TEST(Admission, ANumberFloat32HoldsIsAdmitted) {
  for (const double given : {1.1, 3.4028234663852886e38, -3.4028234663852886e38, 1e-45, 0.0, -0.0,
                             std::numeric_limits<double>::infinity(),
                             -std::numeric_limits<double>::infinity()}) {
    EXPECT_EQ(admits("float32", given), Value(given)) << given;
  }
  EXPECT_TRUE(std::isnan(std::get<double>(admits("float32", std::nan("")))));
  EXPECT_TRUE(std::isnan(std::get<double>(admits("float64", std::nan("")))));
}

TEST(Admission, ANumberFloat32RoundsToAnInfinityOrToZeroIsRefused) {
  for (const double given : {3.4028235677973366e38, -3.5e38, 1e39, 1e-46, -1e-50, 0x1p-150}) {
    EXPECT_EQ(refusal("float32", given), "a number outside float32") << given;
  }
  // Just inside each edge: the largest double that rounds to float's largest finite value, and the
  // smallest that rounds up to its smallest subnormal.
  EXPECT_EQ(refusal("float32", std::nextafter(3.4028235677973366e38, 0.0)), "admitted");
  EXPECT_EQ(refusal("float32", std::nextafter(0x1p-150, 1.0)), "admitted");
}

TEST(Admission, FloatsTakeIntegersAndRefuseOtherKinds) {
  EXPECT_EQ(admits("float64", std::int64_t{7}), Value(7.0));
  EXPECT_EQ(refusal("float64", true), "a value that is not a number");
  EXPECT_EQ(refusal("float64", std::string("1.5")), "a value that is not a number");
  EXPECT_EQ(refusal("float64", sde::Decimal("1.5")), "a value that is not a number");
}

// --- the types whose rule is a filter's ---------------------------------------------------------

TEST(Admission, BoolStringAndBytes) {
  EXPECT_EQ(admits("bool", true), Value(true));
  EXPECT_EQ(refusal("bool", std::int64_t{1}), "a value that is not a boolean");
  EXPECT_EQ(refusal("bool", std::string("true")), "a value that is not a boolean");
  EXPECT_EQ(admits("string", std::string("\xc3\xa9")), Value(std::string("\xc3\xa9")));
  EXPECT_EQ(refusal("string", std::int64_t{7}), "a value that is not text");
  EXPECT_EQ(refusal("string", std::string("\xed\xa0\x80")), "a value that is not text");  // a surrogate
  EXPECT_EQ(refusal("bytes", std::string("ab")), "a value that is not bytes");
}

TEST(Admission, UuidAndDate) {
  EXPECT_EQ(text_of(admits("uuid", std::string("0E984725-C51C-4BF4-9960-E1C80E27ABA0"))),
            "\"0e984725-c51c-4bf4-9960-e1c80e27aba0\"");
  EXPECT_EQ(refusal("uuid", std::string("not-a-uuid")), "a value that is not a UUID");
  EXPECT_EQ(refusal("uuid", std::int64_t{7}), "a value that is not a UUID");
  EXPECT_EQ(text_of(admits("date", std::string("2026-10-07"))), "\"2026-10-07\"");
  EXPECT_EQ(refusal("date", std::string("2026-02-30")), "a value that is not a date");
  EXPECT_EQ(refusal("date", std::int64_t{7}), "a value that is not a date");
}

TEST(Admission, TimestampsAreInstantsInUtcAndWallTimesInUtc) {
  EXPECT_EQ(text_of(admits("timestamptz", std::string("2026-11-09T11:30:15.123456+02:00"))),
            "\"2026-11-09T09:30:15.123456Z\"");
  EXPECT_EQ(text_of(admits("timestamp", std::string("2026-11-09T11:30:15.123456+02:00"))),
            "\"2026-11-09T09:30:15.123456Z\"");
  EXPECT_EQ(text_of(admits("timestamp", std::string("2026-11-09 09:30:15"))), "\"2026-11-09T09:30:15.000000Z\"");
  for (const Value& given : {Value(std::string("2026-10-07T25:00:00Z")),
                             Value(std::string("2026-10-07T10:00:00.1234567Z")),
                             Value(std::string("not a time")), Value(std::int64_t{7})}) {
    EXPECT_EQ(refusal("timestamptz", given), "a value that is not a timestamp");
  }
}

TEST(Admission, JsonIsNotCheckedYet) { EXPECT_FALSE(sde::detail::check_for("json")); }

// --- in a session -------------------------------------------------------------------------------

sde::Model entries() {
  // A delimiter of its own: the type `decimal(12,2)"` would end a plain raw string.
  return sde::load_neutral_model(R"json({"entities": [{"name": "Entry", "fields": [
    {"name": "amount", "type": "decimal(12,2)"},
    {"name": "count", "type": "int32", "nullable": true},
    {"name": "id", "type": "string"}], "key": ["id"]}]})json");
}

sde::PlacementMap placed(const sde::Model& model) {
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {")" + model.entities().front().name +
                           R"(": {"source": {"id": "source", "engine": "pg", "layout": {"auto": true}}}}})";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(std::string_view(text), options);
}

std::string refused_by(const std::function<void()>& body) {
  try {
    body();
  } catch (const sde::BulkWriteRefused& error) {
    return std::string("BulkWriteRefused: ") + error.what();
  } catch (const sde::ModelPlanningError& error) {
    return std::string("ModelPlanningError: ") + error.what();
  }
  return "accepted";
}

TEST(Admission, ASaveIsRefusedBeforeTheEngineAndNamesTheType) {
  const sde::Model model = entries();
  const sde::PlacementMap map = placed(model);
  sde::testing::MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  EXPECT_EQ(refused_by([&] {
              session.save("Entry", {{"amount", std::string("1.239")}, {"count", std::int64_t{1}},
                                     {"id", std::string("e-1")}});
            }),
            "ModelPlanningError: Entry.amount is decimal(12,2) and this row gives it more than 2 "
            "fractional digits");
  EXPECT_EQ(sde::dump_json(pg.recorded().as_json()), "[]");
}

TEST(Admission, TheEngineReceivesTheValueAtItsScaleAndTheIntegerAsGiven) {
  const sde::Model model = entries();
  const sde::PlacementMap map = placed(model);
  sde::testing::MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  session.save("Entry", {{"amount", std::string("1.230")}, {"count", sde::Null{}}, {"id", std::string("e-1")}});
  session.save("Entry", {{"amount", std::int64_t{7}}, {"count", std::int64_t{2}}, {"id", std::string("e-2")}});
  const std::vector<sde::Row>& rows = pg.tables.begin()->second;
  ASSERT_EQ(rows.size(), 2U);
  EXPECT_EQ(sde::dump_json(sde::testing::row_to_json(rows[0])), R"({"amount":"1.23","count":null,"id":"e-1"})");
  EXPECT_EQ(rows[1].at("amount"), Value(std::int64_t{7}));
}

TEST(Admission, ABatchIsRefusedWholeWithTheRowsPosition) {
  const sde::Model model = entries();
  const sde::PlacementMap map = placed(model);
  sde::testing::MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  EXPECT_EQ(refused_by([&] {
              session.save_many("Entry", {{{"amount", std::string("1.23")}, {"count", std::int64_t{1}},
                                           {"id", std::string("e-1")}},
                                          {{"amount", std::string("1.23")},
                                           {"count", std::int64_t{2'147'483'648}},
                                           {"id", std::string("e-2")}}});
            }),
            "BulkWriteRefused: row 1: Entry.count is int32 and this row gives it an integer outside int32");
  EXPECT_EQ(sde::dump_json(pg.recorded().as_json()), "[]");
}

TEST(Admission, ARefusalNamesTheClientsFieldUnderHashedIdentifiers) {
  const sde::Model plain = entries();
  const auto [model, names] = sde::hash_identifiers(plain, std::string(32, 'h'));
  const sde::PlacementMap map = placed(model);
  sde::testing::MemoryEngine pg;
  sde::SessionOptions options;
  options.names = &names;
  sde::Session session(model, map, {{"pg", &pg}}, options);
  EXPECT_EQ(refused_by([&] {
              session.save("Entry", {{"amount", std::string("1.23")}, {"count", true},
                                     {"id", std::string("e-1")}});
            }),
            "ModelPlanningError: Entry.count is int32 and this row gives it a value that is not an integer");
  EXPECT_EQ(sde::dump_json(pg.recorded().as_json()), "[]");
}

}  // namespace
