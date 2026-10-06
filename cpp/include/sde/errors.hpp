#pragma once

/// The library's errors, one hierarchy, named as the format contract and the conformance vectors
/// name them. An `errors/` vector asserts a class by name and a fragment of its message, so these
/// names are part of what the library promises rather than a choice of this port.
///
/// Every message names fields, tables and documents, never a value from a client's row: a message is
/// the one artefact a client pastes into a support ticket.

#include <stdexcept>
#include <string>

namespace sde {

/// The root of every error this library raises on purpose.
class SdeError : public std::runtime_error {
 public:
  explicit SdeError(const std::string& message);
  ~SdeError() override;
  SdeError(const SdeError&) = default;
  SdeError& operator=(const SdeError&) = default;
  SdeError(SdeError&&) = default;
  SdeError& operator=(SdeError&&) = default;
};

/// A value the canonical encoding (format contract section 1) cannot represent.
class CanonicalError : public SdeError {
 public:
  using SdeError::SdeError;
  ~CanonicalError() override;
};

/// A declaration that is not a model: a type outside the vocabulary, a key naming no field, a
/// relation to an entity nobody declared (sections 3, 4a and 8a).
class DeclarationError : public SdeError {
 public:
  using SdeError::SdeError;
  ~DeclarationError() override;
};

/// A valid model that cannot be planned, or an operation the model's structure rules out.
class ModelPlanningError : public SdeError {
 public:
  using SdeError::SdeError;
  ~ModelPlanningError() override;
};

/// A batch of writes refused before anything was sent.
class BulkWriteRefused : public ModelPlanningError {
 public:
  using ModelPlanningError::ModelPlanningError;
  ~BulkWriteRefused() override;
};

/// A placement map the library refuses to load (section 7).
class MapError : public SdeError {
 public:
  using SdeError::SdeError;
  ~MapError() override;
};

/// A signed map older than one already applied to the client's engines (section 7, forward only).
class MapRolledBack : public MapError {
 public:
  using MapError::MapError;
  ~MapRolledBack() override;
};

/// An engine refused, failed or was asked for something it cannot do.
class EngineError : public SdeError {
 public:
  using SdeError::SdeError;
  ~EngineError() override;
};

/// The resource is in use by another holder.
class ResourceBusy : public EngineError {
 public:
  using EngineError::EngineError;
  ~ResourceBusy() override;
};

/// The resource was closed before the call.
class ResourceClosed : public EngineError {
 public:
  using EngineError::EngineError;
  ~ResourceClosed() override;
};

/// A migration step, a map or a packet that would lose or misplace a write.
class MigrationRefused : public SdeError {
 public:
  using SdeError::SdeError;
  ~MigrationRefused() override;
};

/// A logical read the library refuses before any engine is called.
class QueryRefused : public SdeError {
 public:
  using SdeError::SdeError;
  ~QueryRefused() override;
};

}  // namespace sde
