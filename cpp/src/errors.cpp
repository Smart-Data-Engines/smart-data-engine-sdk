#include "sde/errors.hpp"

namespace sde {

// The destructors are defined here so that each class's vtable and type information live in one
// translation unit: a catch clause in a client's binary and a throw in this library then agree on
// one type, which is what lets `catch (const sde::MapError&)` work across a shared-library boundary.

SdeError::SdeError(const std::string& message) : std::runtime_error(message) {}
SdeError::~SdeError() = default;
CanonicalError::~CanonicalError() = default;
DeclarationError::~DeclarationError() = default;
ModelPlanningError::~ModelPlanningError() = default;
BulkWriteRefused::~BulkWriteRefused() = default;
MapError::~MapError() = default;
MapRolledBack::~MapRolledBack() = default;
EngineError::~EngineError() = default;
ResourceBusy::~ResourceBusy() = default;
ResourceClosed::~ResourceClosed() = default;
MigrationRefused::~MigrationRefused() = default;
QueryRefused::~QueryRefused() = default;

}  // namespace sde
