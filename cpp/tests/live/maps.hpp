#pragma once

/// Maps the live tests write: a layout as a map's JSON, and every group of a model in one engine in
/// a dialect's default layout - what the reference builds with `default_layout(..., dialect=...)`.

#include <cstdint>
#include <string>
#include <string_view>
#include <utility>

#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"

namespace sde::live {

/// A layout written out as a map's JSON: tables, columns, and the design it carries.
inline Json layout_json(const PhysicalLayout& layout) {
  Json raw = Json::object();
  Json tables = Json::object();
  for (const auto& [entity, table] : layout.tables) tables.set(entity, table);
  raw.set("tables", std::move(tables));
  Json columns = Json::object();
  for (const auto& [entity, types] : layout.columns) {
    Json typed = Json::object();
    for (const auto& [column, type] : types) typed.set(column, type);
    columns.set(entity, std::move(typed));
  }
  raw.set("columns", std::move(columns));
  return raw;
}

/// A contract-3 map of every group of the model in one engine, each in the dialect's default
/// layout: the reference's `default_layout(model, group, dialect=...)`, written out.
inline PlacementMap default_map(const Model& model, std::string_view dialect,
                                const std::string& engine = "ch") {
  Json groups = Json::object();
  for (const Group& group : model.groups()) {
    Json source = Json::object();
    source.set("id", group.name + "@" + engine);
    source.set("engine", engine);
    source.set("layout", layout_json(default_layout(model, group, dialect)));
    Json placed = Json::object();
    placed.set("source", std::move(source));
    groups.set(group.name, std::move(placed));
  }
  Json document = Json::object();
  document.set("contract", std::int64_t{3});
  document.set("model_version", model.version());
  document.set("map_version", std::int64_t{1});
  document.set("groups", std::move(groups));
  LoadOptions options;
  options.model = &model;
  return load_map(document, options);
}

}  // namespace sde::live
