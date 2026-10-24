#include "sde/watermark.hpp"

#include <algorithm>
#include <string>

#include "python_compat.hpp"
#include "sde/errors.hpp"

namespace sde {

namespace {

using detail::python_repr;

Json strings(const std::vector<std::string>& values) {
  Json out = Json::array();
  for (const std::string& value : values) out.as_array().push_back(value);
  return out;
}

}  // namespace

Json WatermarkCheck::as_record() const {
  Json record = Json::object();
  record.set("protection", protection);
  record.set("map_version", map_version);
  record.set("highest_seen", highest_seen ? Json(*highest_seen) : Json(nullptr));
  record.set("participating", strings(participating));
  record.set("unable", strings(unable));
  record.set("why", why);
  return record;
}

WatermarkCheck enforce_forward_only(const PlacementMap& placement,
                                    const std::map<std::string, Engine*>& engines,
                                    const LogSink& log) {
  WatermarkCheck check;
  check.map_version = placement.map_version();
  if (!placement.is_signed()) {
    // The no-account mode: the client's own document, and nothing is read, written or created.
    check.protection = "not_applicable";
    for (const auto& [name, unused] : engines) check.unable.push_back(name);
    check.why =
        "this map is unsigned, so it is your own document rather than one we issued. Replacing it "
        "with another is the no-account mode working as documented, and there is no newest "
        "version for us to be the authority on.";
    return check;
  }

  std::vector<std::pair<std::string, WatermarkStore*>> able;
  for (const auto& [name, engine] : engines) {
    if (WatermarkStore* store = engine->capabilities().watermark) {
      able.emplace_back(name, store);
      check.participating.push_back(name);
    } else {
      check.unable.push_back(name);
    }
  }
  if (able.empty()) {
    check.protection = "unavailable";
    check.why = "none of the engines in this map can keep bookkeeping (" +
                python_repr(check.unable) +
                "), so a map that goes backwards cannot be recognised. An engine whose schema is "
                "fixed in its own source - ours is - has nowhere to put it. Nothing is wrong with "
                "your configuration; this protection simply does not exist for it.";
    Json fields = Json::object();
    fields.set("map_version", placement.map_version());
    fields.set("engines", static_cast<std::int64_t>(check.unable.size()));
    detail::emit(log, "sde.map.rollback_unprotected", fields);
    return check;
  }

  std::optional<std::int64_t> highest;
  for (const auto& [name, store] : able) {
    const std::optional<std::int64_t> seen = store->map_watermark();
    if (seen && (!highest || *seen > *highest)) highest = seen;
  }
  if (highest && placement.map_version() < *highest) {
    throw MapRolledBack(
        "this map is version " + std::to_string(placement.map_version()) + " and version " +
        std::to_string(*highest) +
        " has already been applied against these engines. Refusing to go backwards: an older "
        "signed map verifies perfectly - that is what a signature is - so nothing else here would "
        "notice that the file was replaced, and the writes would go to the previous placement. If "
        "this is deliberate, because the newer map was wrong, clear the bookkeeping: DELETE FROM " +
        std::string(WATERMARK_TABLE) + " WHERE map_version > " +
        std::to_string(placement.map_version()) + "; in " + python_repr(check.participating) +
        ", and on an engine that deletes asynchronously, let the deletion finish before "
        "restarting. That is a deliberate act with a stated consequence, which is why it is not a "
        "flag.");
  }
  if (!highest || placement.map_version() > *highest) {
    // Written only when it moves: recording every start would grow the table by a row per restart
    // and say nothing more.
    for (const auto& [name, store] : able) {
      store->record_map_version(placement.map_version(), placement.model_version());
    }
  }
  Json fields = Json::object();
  fields.set("map_version", placement.map_version());
  fields.set("highest_seen", highest ? Json(*highest) : Json(nullptr));
  fields.set("engines", static_cast<std::int64_t>(able.size()));
  detail::emit(log, "sde.map.forward_only", fields);

  check.protection = "enforced";
  check.highest_seen = highest;
  check.why = "the highest map version applied against these engines is " +
              std::to_string(highest ? *highest : placement.map_version()) + ", kept in " +
              std::string(WATERMARK_TABLE) + " in " + python_repr(check.participating) +
              ". A map older than that is refused.";
  return check;
}

}  // namespace sde
