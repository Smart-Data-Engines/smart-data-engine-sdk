/// `query/` (Tier 2): the logical read plan and the exact summary, compared as documents and as the
/// canonical bytes of that document. Every library writes a plan's values the same way here - a
/// decimal at its own scale, an instant in UTC with six digits, a float as the hex of its IEEE bits
/// - so a disagreement about a value is a red case rather than two plausible texts.

#include <bit>
#include <cstdint>
#include <cstdio>
#include <string>
#include <type_traits>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "sde/query.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

// `query/012` gives a page limit as `true`, which the reference refuses when the read is planned.
// Here the refusal happens earlier: a `bool` does not convert to a page limit, so the call cannot
// be written. The runner holds that claim to this assertion rather than to the vector's word.
static_assert(!std::is_constructible_v<sde::PageLimit, bool>);
static_assert(std::is_constructible_v<sde::PageLimit, int>);

/// The vector's value encoding: `{"$int": "..."}` for an integer past 2^53, `{"$bytes": "hex"}` for
/// bytes, and plain JSON otherwise.
sde::Value decode(const sde::Json& value) {
  switch (value.kind()) {
    case sde::Json::Kind::null:
      return sde::Null{};
    case sde::Json::Kind::boolean:
      return value.as_bool();
    case sde::Json::Kind::string:
      return value.as_string();
    case sde::Json::Kind::number:
      if (const auto integer = value.to_int64()) return *integer;
      return *value.to_double();
    case sde::Json::Kind::object:
      if (const sde::Json* integer = value.find("$int")) {
        return std::stoll(integer->as_string());
      }
      if (const sde::Json* bytes = value.find("$bytes")) {
        return *sde::Bytes::from_hex(bytes->as_string());
      }
      break;
    case sde::Json::Kind::array:
      break;
  }
  ADD_FAILURE() << "a query vector value this runner cannot decode: " << sde::dump_json(value);
  return sde::Null{};
}

sde::Row decode_row(const sde::Json& object) {
  sde::Row row;
  for (const auto& [name, value] : object.as_object()) row[name] = decode(value);
  return row;
}

/// A plan's value as the vectors write it; the same rules as both reference runners.
sde::Json normalized(const sde::ReadColumn& column, const sde::Value& value) {
  if (sde::is_null(value)) return nullptr;
  if (column.type == "bool") return std::get<bool>(value);
  if (column.type.rfind("float", 0) == 0) {
    char hex[17];
    std::snprintf(hex, sizeof hex, "%016llx",
                  static_cast<unsigned long long>(std::bit_cast<std::uint64_t>(std::get<double>(value))));
    return std::string(hex);
  }
  if (const auto* decimal = std::get_if<sde::Decimal>(&value)) return decimal->to_string();
  if (const auto* naive = std::get_if<sde::Timestamp>(&value)) return naive->to_string() + "Z";
  if (const auto* instant = std::get_if<sde::TimestampTz>(&value)) return instant->to_string();
  if (const auto* bytes = std::get_if<sde::Bytes>(&value)) return bytes->to_hex();
  if (const auto* integer = std::get_if<std::int64_t>(&value)) return std::to_string(*integer);
  if (const auto* text = std::get_if<std::string>(&value)) return *text;
  if (const auto* uuid = std::get_if<sde::Uuid>(&value)) return uuid->to_string();
  if (const auto* date = std::get_if<sde::Date>(&value)) return date->to_string();
  ADD_FAILURE() << "a plan value of kind " << sde::value_kind(value) << " for " << column.type;
  return nullptr;
}

sde::Json optional_text(const std::optional<sde::Decimal>& value) {
  return value ? sde::Json(value->to_string()) : sde::Json(nullptr);
}

sde::Json run_summary(const sde::Json& fixture) {
  const sde::Json& record = *fixture.find("record");
  const auto text = [&record](std::string_view key) -> std::optional<std::string> {
    const sde::Json* value = record.find(key);
    if (value == nullptr || value->is_null()) return std::nullopt;
    return value->as_string();
  };
  sde::SummaryRecord raw;
  // A case may give no record at all (`query/023`): the column is refused before the record is
  // read, and an empty count here would be refused as the engine's error if it were not.
  raw.count = text("sde_count").value_or("");
  raw.present = text("sde_present").value_or("");
  raw.minimum = text("sde_min");
  raw.maximum = text("sde_max");
  raw.total = text("sde_total");
  const sde::Json* scale = fixture.find("mean_scale");
  const sde::NumericSummary summary = sde::numeric_summary(
      raw, sde::ReadColumn{"value", fixture.find("type")->as_string()},
      scale == nullptr ? 6 : static_cast<int>(*scale->to_int64()));
  sde::Json out = sde::Json::object();
  out.set("count", std::to_string(summary.count));
  out.set("non_null_count", std::to_string(summary.non_null_count));
  out.set("minimum", optional_text(summary.minimum));
  out.set("maximum", optional_text(summary.maximum));
  out.set("total", optional_text(summary.total));
  out.set("mean", optional_text(summary.mean));
  return out;
}

sde::Json run_plan(const sde::Json& fixture) {
  std::vector<sde::ReadColumn> columns;
  for (const auto& [name, type] : fixture.find("columns")->as_object()) {
    columns.push_back(sde::ReadColumn{name, type.as_string()});
  }
  std::vector<std::string> key;
  for (const sde::Json& name : fixture.find("key")->as_array()) key.push_back(name.as_string());

  sde::ReadOptions options;
  if (const sde::Json* given = fixture.find("options")) {
    for (const auto& [name, value] : given->as_object()) {
      if (name == "where") {
        options.where = decode_row(value);
      } else if (name == "bounds") {
        sde::Range range;
        range.field = value.find("field")->as_string();
        if (const sde::Json* low = value.find("low")) range.low = decode(*low);
        if (const sde::Json* high = value.find("high")) range.high = decode(*high);
        options.bounds = range;
      } else if (name == "order_by") {
        if (!value.is_null()) options.order_by = value.as_string();
      } else if (name == "descending") {
        options.descending = value.as_bool();
      } else if (name == "after") {
        if (!value.is_null()) options.after = decode_row(value);
      } else if (name == "limit") {
        if (value.is_bool()) {
          // Unrepresentable: see the static_assert above. The reference's refusal, reached by
          // the type system instead of at run time.
          sde::Json refused = sde::Json::object();
          refused.set("error", "QueryRefused");
          return refused;
        }
        options.limit = *value.to_int64();
      } else if (name == "paginate") {
        options.paginate = value.as_bool();
      } else {
        ADD_FAILURE() << "a read option this runner does not know: " << name;
      }
    }
  }
  const sde::ReadPlan plan = sde::plan_read(columns, key, options);
  sde::Json filters = sde::Json::array();
  for (const sde::ReadFilter& filter : plan.filters) {
    sde::Json item = sde::Json::object();
    item.set("field", filter.column.name);
    item.set("type", filter.column.type);
    item.set("op", std::string(sde::operation_name(filter.operation)));
    item.set("value", normalized(filter.column, filter.value));
    filters.as_array().push_back(std::move(item));
  }
  sde::Json order = sde::Json::array();
  for (const sde::ReadColumn& column : plan.order) order.as_array().push_back(column.name);
  sde::Json after = nullptr;
  if (plan.after) {
    after = sde::Json::array();
    for (std::size_t i = 0; i < plan.order.size(); ++i) {
      after.as_array().push_back(normalized(plan.order[i], (*plan.after)[i]));
    }
  }
  sde::Json out = sde::Json::object();
  out.set("filters", std::move(filters));
  out.set("order", std::move(order));
  out.set("descending", plan.descending);
  out.set("after", std::move(after));
  out.set("limit", plan.limit);
  return out;
}

class QueryVector : public ::testing::TestWithParam<std::string> {};

TEST_P(QueryVector, PlansOrSummarisesExactly) {
  const auto directory = vectors_root() / "query" / GetParam();
  const sde::Json fixture = read_json(directory / "case.json");
  sde::Json got;
  try {
    got = fixture.find("kind")->as_string() == "summary" ? run_summary(fixture) : run_plan(fixture);
  } catch (const sde::QueryRefused&) {
    got = sde::Json::object();
    got.set("error", "QueryRefused");
  }
  EXPECT_EQ(got, read_json(directory / "expected.json")) << sde::dump_json(got);
  const std::string bytes = sde::canonical_bytes(got);
  EXPECT_EQ(sde::Bytes{std::vector<std::uint8_t>(bytes.begin(), bytes.end())}.to_hex(),
            read_text(directory / "expected.hex"));
}

INSTANTIATE_TEST_SUITE_P(Query, QueryVector, ::testing::ValuesIn(cases("query")),
                         [](const auto& param_info) { return test_name(param_info.param); });

}  // namespace
