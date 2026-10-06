#include "sde/verification.hpp"

#include <algorithm>
#include <cmath>
#include <set>
#include <string>

#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "value_internal.hpp"

namespace sde {

namespace {

constexpr std::int64_t kMaxSafe = 9'007'199'254'740'991;

bool lower_hex(std::string_view text, std::size_t width) {
  return text.size() == width && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

void hex_field(const std::string& value, std::size_t width, std::string_view field) {
  if (!lower_hex(value, width)) {
    throw MigrationRefused("verification " + std::string(field) + " must be " +
                           std::to_string(width) + " lowercase hexadecimal digits");
  }
}

void name_field(const std::string& value) {
  if (value.empty()) throw MigrationRefused("verification names must be nonempty strings");
}

/// A JSON value where a string belongs: the reference refuses a non-string with the same message
/// as a malformed one, by the field's own rule, so a non-string is passed on as a value that rule
/// refuses.
std::string text_of(const Json& value, std::string_view refused) {
  return value.is_string() ? value.as_string() : std::string(refused);
}

/// `_integer`: an integral JSON number in the safe range, however it is written (`1.0` is `1`).
std::int64_t safe_integer(const Json& value, std::string_view field) {
  const auto refuse = [field] {
    return MigrationRefused("verification " + std::string(field) +
                            " must be a positive safe integer");
  };
  if (!value.is_number()) throw refuse();
  if (const auto integer = value.to_int64()) {
    if (*integer < 1 || *integer > kMaxSafe) throw refuse();
    return *integer;
  }
  const double number = detail::python_float(value.as_number().lexeme);
  if (!std::isfinite(number) || number < 1 || number > static_cast<double>(kMaxSafe) ||
      std::trunc(number) != number) {
    throw refuse();
  }
  return static_cast<std::int64_t>(number);
}

bool exactly(const Json& object, std::initializer_list<std::string_view> keys) {
  if (!object.is_object() || object.as_object().size() != keys.size()) return false;
  return std::all_of(keys.begin(), keys.end(), [&](std::string_view key) { return object.contains(key); });
}

/// Two ASCII digits at a position.
std::optional<int> two(std::string_view text, std::size_t at) {
  if (at + 2 > text.size()) return std::nullopt;
  const char a = text[at];
  const char b = text[at + 1];
  if (a < '0' || a > '9' || b < '0' || b > '9') return std::nullopt;
  return (a - '0') * 10 + (b - '0');
}

}  // namespace

std::int64_t aware_time(std::string_view value) {
  const MigrationRefused refused("verification time must be an ISO timestamp with an offset");
  // \d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)
  if (value.size() < 20 || value[4] != '-' || value[7] != '-' || value[10] != 'T' ||
      value[13] != ':' || value[16] != ':') {
    throw refused;
  }
  int year = 0;
  for (std::size_t i = 0; i < 4; ++i) {
    if (value[i] < '0' || value[i] > '9') throw refused;
    year = year * 10 + (value[i] - '0');
  }
  const auto month = two(value, 5);
  const auto day = two(value, 8);
  const auto hour = two(value, 11);
  const auto minute = two(value, 14);
  const auto second = two(value, 17);
  if (!month || !day || !hour || !minute || !second || *hour > 23 || *minute > 59 ||
      *second > 59) {
    throw refused;
  }
  std::size_t i = 19;
  std::int64_t fraction = 0;
  if (value[i] == '.') {
    ++i;
    std::size_t count = 0;
    while (i < value.size() && value[i] >= '0' && value[i] <= '9') {
      if (++count > 6) throw refused;
      fraction = fraction * 10 + (value[i] - '0');
      ++i;
    }
    if (count == 0) throw refused;
    for (std::size_t k = count; k < 6; ++k) fraction *= 10;
  }
  std::int64_t offset = 0;
  if (i < value.size() && value[i] == 'Z') {
    ++i;
  } else if (i < value.size() && (value[i] == '+' || value[i] == '-')) {
    const bool negative = value[i] == '-';
    const auto offset_hour = two(value, i + 1);
    const auto offset_minute = two(value, i + 4);
    if (!offset_hour || !offset_minute || i + 3 >= value.size() || value[i + 3] != ':' ||
        *offset_hour > 23 || *offset_minute > 59) {
      throw refused;
    }
    offset = (*offset_hour * 60 + *offset_minute) * 60;
    if (negative) offset = -offset;
    i += 6;
  } else {
    throw refused;
  }
  if (i != value.size()) throw refused;
  const auto days =
      detail::days_from_civil(year, static_cast<unsigned>(*month), static_cast<unsigned>(*day));
  if (!days) throw refused;
  const std::int64_t local = ((*days * 24 + *hour) * 60 + *minute) * 60 + *second;
  return (local - offset) * detail::kMicrosPerSecond + fraction;
}

VerificationRequest::VerificationRequest(std::string request_id, std::string project_id,
                                         std::string model_version, std::int64_t map_version,
                                         std::string map_fingerprint, std::string group,
                                         std::string source_engine, std::string source_id,
                                         std::vector<std::pair<std::string, std::string>> targets,
                                         std::string requested_at, bool requires_signature)
    : request_id_(std::move(request_id)),
      project_id_(std::move(project_id)),
      model_version_(std::move(model_version)),
      map_version_(map_version),
      map_fingerprint_(std::move(map_fingerprint)),
      group_(std::move(group)),
      source_engine_(std::move(source_engine)),
      source_id_(std::move(source_id)),
      targets_(std::move(targets)),
      requested_at_(std::move(requested_at)),
      requires_signature_(requires_signature) {
  hex_field(request_id_, 32, "request_id");
  hex_field(project_id_, 32, "project_id");
  hex_field(model_version_, 16, "model_version");
  hex_field(map_fingerprint_, 64, "map_fingerprint");
  if (map_version_ < 1 || map_version_ > kMaxSafe) {
    throw MigrationRefused("verification map_version must be a positive integer");
  }
  name_field(group_);
  name_field(source_engine_);
  name_field(source_id_);
  if (targets_.empty()) {
    throw MigrationRefused("verification targets must be nonempty engine/id pairs");
  }
  std::set<std::string> identities;
  for (const auto& [engine, identity] : targets_) {
    name_field(engine);
    name_field(identity);
    if (identity == source_id_) {
      throw MigrationRefused("verification source cannot also be a target");
    }
    identities.insert(identity);
  }
  if (identities.size() != targets_.size()) {
    throw MigrationRefused("verification target ids must be unique");
  }
  if (!std::is_sorted(targets_.begin(), targets_.end())) {
    throw MigrationRefused("verification targets must be sorted by engine and id");
  }
  (void)aware_time(requested_at_);
}

VerificationRequest VerificationRequest::from_record(const Json& record) {
  if (!exactly(record, {"protocol", "request_id", "project_id", "model_version", "map_version",
                        "map_fingerprint", "group", "source", "targets", "requested_at",
                        "requires_signature"})) {
    throw MigrationRefused("verification request has missing or unknown fields");
  }
  const Json& protocol = *record.find("protocol");
  if (!protocol.is_number() || protocol != Json(REQUEST_PROTOCOL)) {
    throw MigrationRefused("unsupported verification request protocol");
  }
  const Json& source = *record.find("source");
  const Json& targets = *record.find("targets");
  if (!exactly(source, {"engine", "id"})) {
    throw MigrationRefused("verification source must contain exactly engine and id");
  }
  if (!targets.is_array() ||
      std::any_of(targets.as_array().begin(), targets.as_array().end(),
                  [](const Json& target) { return !exactly(target, {"engine", "id"}); })) {
    throw MigrationRefused("verification targets must contain exactly engine and id");
  }
  const std::int64_t map_version = safe_integer(*record.find("map_version"), "map_version");
  // What follows is the constructor's order of checks; a value of the wrong JSON type is handed on
  // as one the field's own rule refuses, so the refusal names the same rule as the reference's.
  const std::string request_id = text_of(*record.find("request_id"), "");
  const std::string project_id = text_of(*record.find("project_id"), "");
  const std::string model_version = text_of(*record.find("model_version"), "");
  const std::string map_fingerprint = text_of(*record.find("map_fingerprint"), "");
  hex_field(request_id, 32, "request_id");
  hex_field(project_id, 32, "project_id");
  hex_field(model_version, 16, "model_version");
  hex_field(map_fingerprint, 64, "map_fingerprint");
  const Json& signature = *record.find("requires_signature");
  if (!signature.is_bool()) {
    throw MigrationRefused("verification requires_signature must be a boolean");
  }
  const auto name = [](const Json& value) { return text_of(value, ""); };
  const std::string group = name(*record.find("group"));
  const std::string source_engine = name(*source.find("engine"));
  const std::string source_id = name(*source.find("id"));
  name_field(group);
  name_field(source_engine);
  name_field(source_id);
  std::vector<std::pair<std::string, std::string>> pairs;
  for (const Json& target : targets.as_array()) {
    pairs.emplace_back(name(*target.find("engine")), name(*target.find("id")));
  }
  // The time is the constructor's last check, after the targets: a non-string is handed on as text
  // the time's own rule refuses.
  return VerificationRequest(request_id, project_id, model_version, map_version, map_fingerprint,
                             group, source_engine, source_id, std::move(pairs),
                             text_of(*record.find("requested_at"), ""), signature.as_bool());
}

Json VerificationRequest::as_record() const {
  Json record = Json::object();
  record.set("protocol", REQUEST_PROTOCOL);
  record.set("request_id", request_id_);
  record.set("project_id", project_id_);
  record.set("model_version", model_version_);
  record.set("map_version", map_version_);
  record.set("map_fingerprint", map_fingerprint_);
  record.set("group", group_);
  Json source = Json::object();
  source.set("engine", source_engine_);
  source.set("id", source_id_);
  record.set("source", std::move(source));
  Json targets = Json::array();
  for (const auto& [engine, identity] : targets_) {
    Json target = Json::object();
    target.set("engine", engine);
    target.set("id", identity);
    targets.as_array().push_back(std::move(target));
  }
  record.set("targets", std::move(targets));
  record.set("requested_at", requested_at_);
  record.set("requires_signature", requires_signature_);
  return record;
}

void VerificationRequest::check_session(const PlacementMap& placement,
                                        const std::optional<std::string>& project_id,
                                        const std::string& group) const {
  if (project_id != project_id_) {
    throw MigrationRefused(
        "verification request names another project, or this session has no project_id. "
        "Configure the project id from the client's enrollment, not from the request.");
  }
  if (group != group_) throw MigrationRefused("verification request names another group");
  const VerificationRequest expected =
      verification_request(placement, group, *project_id, request_id_, requested_at_);
  if (!(*this == expected)) {
    throw MigrationRefused(
        "verification request does not match this session's model, map, source or targets; no "
        "comparison was started");
  }
}

void VerificationRequest::check_time(const std::string& at) const {
  if (aware_time(at) < aware_time(requested_at_)) {
    throw MigrationRefused(
        "verification predates its request; check the verifier's clock and run the comparison "
        "for the current request");
  }
}

VerificationRequest verification_request(const PlacementMap& placement, const std::string& group,
                                         const std::string& project_id,
                                         const std::string& request_id,
                                         const std::string& requested_at) {
  if (!placement.fingerprint()) {
    throw MigrationRefused("verification needs a loaded, canonically encodable placement map");
  }
  const GroupPlacement& spot = placement.placement_of(group);
  std::vector<std::pair<std::string, std::string>> targets;
  for (const Materialization* target : spot.also_write_targets()) {
    targets.emplace_back(target->engine, target->id);
  }
  std::sort(targets.begin(), targets.end());
  return VerificationRequest(request_id, project_id, placement.model_version(),
                             placement.map_version(), *placement.fingerprint(), group,
                             spot.source.engine, spot.source.id, std::move(targets), requested_at,
                             placement.is_signed());
}

}  // namespace sde
