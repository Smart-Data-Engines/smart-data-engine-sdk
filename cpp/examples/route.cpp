// Declares a model in C++, loads a hand-written placement map - the no-account mode: unsigned, no
// key, no network - and asks where each operation of the model goes. Built and run by the test
// suite, so the excerpt of it in cpp/README.md cannot stop compiling unnoticed.

#include <iostream>
#include <string>
#include <vector>

#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/routing.hpp"

int main() {
  const sde::Model model =
      sde::ModelBuilder{}
          .entity({"Order", {{"id", "uuid"}, {"placed", "timestamptz"}, {"total", "decimal(12,2)"}},
                   {"id"}})
          .entity({"Fill", {{"id", "uuid"}, {"order_id", "uuid"}, {"qty", "int64"}}, {"id"}})
          .relation("order", "Fill", "Order")
          .build();

  // The shape of "orders in a time range": what an analytical copy is for.
  const sde::OperationShape* ranges = nullptr;
  for (const sde::OperationShape& shape : model.shapes()) {
    if (shape.entity == "Order" && shape.kind == "range_read" &&
        shape.fields == std::vector<std::string>{"placed"}) {
      ranges = &shape;
    }
  }
  if (ranges == nullptr) return 1;

  // One colocation group, named after its first member. The source is in PostgreSQL with a layout
  // derived from the model; a copy in ClickHouse serves the range reads.
  const std::string map_text = R"({"contract": 3, "model_version": ")" + model.version() +
                               R"(", "map_version": 1, "groups": {"Fill": {
      "source": {"id": "orders@pg", "engine": "pg-main", "layout": {"auto": true}},
      "derived": [{"id": "orders@ch", "engine": "ch-analytics", "lag_budget_ms": 30000,
                   "layout": {"tables": {"Fill": "fill", "Order": "order_wide"}}}]}},
    "routing": {")" + ranges->id + R"(": "orders@ch"}})";

  sde::LoadOptions options;
  options.model = &model;  // a signed map would also need options.public_keys
  const sde::PlacementMap map = sde::load_map(map_text, options);

  for (const sde::OperationShape& shape : model.shapes()) {
    const sde::Materialization& copy = sde::resolve(map, shape);
    std::cout << shape.entity << ' ' << shape.kind;
    for (const std::string& field : shape.fields) std::cout << ' ' << field;
    std::cout << " -> " << copy.id << " (" << copy.engine << ", table "
              << copy.layout.table_for(shape.entity) << ")\n";
  }

  // A write goes to the source, and so does a read inside a transaction that has written: a copy
  // is behind by design and cannot show it.
  const bool routed = sde::resolve(map, *ranges).id == "orders@ch";
  const bool own_write = sde::resolve(map, *ranges, {.in_write_transaction = true}).id == "orders@pg";
  return routed && own_write ? 0 : 1;
}
