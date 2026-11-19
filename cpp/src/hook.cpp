#include "sde/hook.hpp"

#include "sde/errors.hpp"
#include "sde/json.hpp"

namespace sde {

namespace {

/// The most derived class of the hierarchy an error is, by name - the names `errors/` vectors use.
std::string hierarchy_class(const SdeError& error) {
  if (dynamic_cast<const ResourceBusy*>(&error) != nullptr) return "ResourceBusy";
  if (dynamic_cast<const ResourceClosed*>(&error) != nullptr) return "ResourceClosed";
  if (dynamic_cast<const EngineError*>(&error) != nullptr) return "EngineError";
  if (dynamic_cast<const MapRolledBack*>(&error) != nullptr) return "MapRolledBack";
  if (dynamic_cast<const MapError*>(&error) != nullptr) return "MapError";
  if (dynamic_cast<const BulkWriteRefused*>(&error) != nullptr) return "BulkWriteRefused";
  if (dynamic_cast<const QueryRefused*>(&error) != nullptr) return "QueryRefused";
  if (dynamic_cast<const ModelPlanningError*>(&error) != nullptr) return "ModelPlanningError";
  if (dynamic_cast<const DeclarationError*>(&error) != nullptr) return "DeclarationError";
  if (dynamic_cast<const CanonicalError*>(&error) != nullptr) return "CanonicalError";
  if (dynamic_cast<const MigrationRefused*>(&error) != nullptr) return "MigrationRefused";
  if (dynamic_cast<const JsonError*>(&error) != nullptr) return "JsonError";
  return "SdeError";
}

}  // namespace

std::string outcome_of(const std::exception_ptr& error) {
  if (!error) return "ok";
  try {
    std::rethrow_exception(error);
  } catch (const SdeError& known) {
    return hierarchy_class(known);
  } catch (...) {
    return "unexpected";
  }
}

}  // namespace sde
