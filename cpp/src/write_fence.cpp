#include "sde/write_fence.hpp"

#include <algorithm>
#include <cmath>
#include <string>

#include "python_compat.hpp"
#include "write_fence_internal.hpp"
#include "sde/errors.hpp"
#include "sde/placement.hpp"

namespace sde {

namespace {

using detail::python_space_at;
using detail::python_strip;

bool starts_with(std::string_view text, std::string_view prefix) noexcept {
  return text.substr(0, prefix.size()) == prefix;
}

bool lower_hex(std::string_view text, std::size_t length) noexcept {
  return text.size() == length && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

void identity(std::string_view value, std::string_view label) {
  if (!lower_hex(value, 32)) {
    throw MigrationRefused("write fence " + std::string(label) +
                           " must be 32 lowercase hexadecimal digits");
  }
}

std::size_t skip_spaces(std::string_view text, std::size_t pos) noexcept {
  while (pos < text.size()) {
    const std::size_t length = python_space_at(text, pos);
    if (length == 0) break;
    pos += length;
  }
  return pos;
}

std::size_t skip_char(std::string_view text, std::size_t pos, char c) noexcept {
  while (pos < text.size() && text[pos] == c) ++pos;
  return pos;
}

/// `re.sub(r"\s+NOT\s+VALID$", "", value)`: the text without a trailing `NOT VALID`.
std::string_view without_not_valid(std::string_view value) {
  constexpr std::string_view kValid = "VALID";
  constexpr std::string_view kNot = "NOT";
  if (value.size() < kValid.size() || value.substr(value.size() - kValid.size()) != kValid) {
    return value;
  }
  // Walk back over whole code points of whitespace: at least one between NOT and VALID, at least one
  // before NOT, and the match starts where the run before NOT starts.
  const auto spaces_before = [value](std::size_t end) {
    std::size_t start = end;
    while (start > 0) {
      std::size_t candidate = start - 1;
      while (candidate > 0 && start - candidate < 4 &&
             (static_cast<unsigned char>(value[candidate]) & 0xC0U) == 0x80U) {
        --candidate;
      }
      if (python_space_at(value, candidate) != start - candidate) break;
      start = candidate;
    }
    return start;
  };
  const std::size_t valid_start = value.size() - kValid.size();
  const std::size_t gap = spaces_before(valid_start);
  if (gap == valid_start || gap < kNot.size() || value.substr(gap - kNot.size(), kNot.size()) != kNot) {
    return value;
  }
  const std::size_t not_start = gap - kNot.size();
  const std::size_t run = spaces_before(not_start);
  if (run == not_start) return value;
  return value.substr(0, run);
}

std::string without_leading_zeros(std::string_view digits) {
  const std::size_t first = digits.find_first_not_of('0');
  return first == std::string_view::npos ? std::string("0") : std::string(digits.substr(first));
}

}  // namespace

std::string detail::fence_predicate(std::string_view raw) {
  std::string_view value = python_strip(raw);
  if (starts_with(value, "CHECK")) value = python_strip(value.substr(5));
  value = without_not_valid(value);
  while (!value.empty() && value.front() == '(' && value.back() == ')') {
    value = python_strip(value.substr(1, value.size() - 2));
  }
  if (value == "true" || value == "1") return "1";
  if (value == "false" || value == "0") return "0";

  // \(*\s*(?:"__sde_write_epoch"|`__sde_write_epoch`|__sde_write_epoch)\s*\)*\s*(>=|<=)
  // \s*\(*\s*(?:([0-9]+)|'([0-9]+)'::bigint)\s*\)*
  const std::string column(EPOCH_COLUMN);
  std::size_t pos = skip_spaces(value, skip_char(value, 0, '('));
  bool named = false;
  for (const std::string& spelling : {"\"" + column + "\"", "`" + column + "`", column}) {
    if (value.substr(pos, spelling.size()) == spelling) {
      pos += spelling.size();
      named = true;
      break;
    }
  }
  if (!named) return "<unrecognized>";
  pos = skip_spaces(value, skip_char(value, skip_spaces(value, pos), ')'));
  const std::string_view operation = value.substr(pos, 2);
  if (operation != ">=" && operation != "<=") return "<unrecognized>";
  pos = skip_spaces(value, skip_char(value, skip_spaces(value, pos + 2), '('));
  const bool quoted = pos < value.size() && value[pos] == '\'';
  if (quoted) ++pos;
  const std::size_t digits_start = pos;
  while (pos < value.size() && value[pos] >= '0' && value[pos] <= '9') ++pos;
  if (pos == digits_start) return "<unrecognized>";
  const std::string_view digits = value.substr(digits_start, pos - digits_start);
  if (quoted) {
    constexpr std::string_view kCast = "'::bigint";
    if (value.substr(pos, kCast.size()) != kCast) return "<unrecognized>";
    pos += kCast.size();
  }
  pos = skip_char(value, skip_spaces(value, pos), ')');
  if (pos != value.size()) return "<unrecognized>";
  return column + std::string(operation) + without_leading_zeros(digits);
}

namespace {

/// `int(text)` of ASCII digits, as an epoch: refused past the safe range like any other epoch.
std::int64_t epoch_digits(std::string_view digits) {
  const std::string trimmed = without_leading_zeros(digits);
  if (trimmed.size() > 16) return check_epoch(MAX_EPOCH + 1);
  return check_epoch(std::stoll(trimmed));
}

bool matches_hex_suffix(std::string_view suffix, std::string_view prefix) {
  return starts_with(suffix, prefix) && lower_hex(suffix.substr(prefix.size()), 32);
}

/// `(?:min|max)_[1-9][0-9]*`
bool bound_suffix(std::string_view suffix) {
  if (!starts_with(suffix, "min_") && !starts_with(suffix, "max_")) return false;
  const std::string_view digits = suffix.substr(4);
  return !digits.empty() && digits.front() >= '1' && digits.front() <= '9' &&
         std::all_of(digits.begin(), digits.end(), [](char c) { return c >= '0' && c <= '9'; });
}

Json optional_epoch(const std::optional<std::int64_t>& value) {
  return value ? Json(*value) : Json(nullptr);
}

Json strings(const std::vector<std::string>& values) {
  Json out = Json::array();
  for (const std::string& value : values) out.as_array().push_back(value);
  return out;
}

}  // namespace

std::string_view column_state_name(ColumnState state) noexcept {
  switch (state) {
    case ColumnState::absent:
      return "absent";
    case ColumnState::valid:
      return "valid";
    case ColumnState::conflict:
      return "conflict";
  }
  return "absent";
}

std::optional<std::int64_t> FenceState::lower_epoch() const {
  if (minimums.empty()) return std::nullopt;
  return *std::max_element(minimums.begin(), minimums.end());
}

std::optional<std::int64_t> FenceState::upper_epoch() const {
  if (maximums.empty()) return std::nullopt;
  return *std::min_element(maximums.begin(), maximums.end());
}

bool FenceState::complete() const {
  return project_id.has_value() && column == ColumnState::valid && !minimums.empty() &&
         !maximums.empty();
}

bool FenceState::closed() const {
  const auto low = lower_epoch();
  const auto high = upper_epoch();
  return !holds.empty() || (low && high && *low > *high);
}

std::optional<std::int64_t> FenceState::epoch() const {
  if (complete() && lower_epoch() == upper_epoch()) return lower_epoch();
  return std::nullopt;
}

Json FenceState::as_record() const {
  Json record = Json::object();
  record.set("identity", identity);
  record.set("project_id", project_id ? Json(*project_id) : Json(nullptr));
  record.set("column", std::string(column_state_name(column)));
  record.set("lower_epoch", optional_epoch(lower_epoch()));
  record.set("upper_epoch", optional_epoch(upper_epoch()));
  record.set("holds", strings(holds));
  record.set("retired", strings(retired));
  record.set("closed", closed());
  return record;
}

std::int64_t check_epoch(std::int64_t epoch) {
  if (epoch < 1 || epoch > MAX_EPOCH) {
    throw MigrationRefused("write epoch must be a positive safe integer");
  }
  return epoch;
}

std::int64_t epoch_from_json(const Json& value) {
  if (!value.is_number()) throw MigrationRefused("write epoch must be a positive safe integer");
  if (const auto integer = value.to_int64()) return check_epoch(*integer);
  // `1.0` is `1`: the integral value is what counts, however it is spelled, as in map contract 4.
  const double number = detail::python_float(value.as_number().lexeme);
  if (!std::isfinite(number) || number < 1 || number > static_cast<double>(MAX_EPOCH) ||
      std::trunc(number) != number) {
    throw MigrationRefused("write epoch must be a positive safe integer");
  }
  return check_epoch(static_cast<std::int64_t>(number));
}

FenceState fence_state(const FenceMetadata& metadata) {
  std::vector<std::string> owners;
  FenceState state;
  for (const auto& [name, raw] : metadata.constraints) {
    if (!starts_with(name, FENCE_PREFIX)) continue;
    const std::string_view suffix = std::string_view(name).substr(FENCE_PREFIX.size());
    const std::string normalised = detail::fence_predicate(raw);
    std::string expected;
    if (matches_hex_suffix(suffix, "owner_")) {
      owners.emplace_back(suffix.substr(6));
      expected = "1";
    } else if (matches_hex_suffix(suffix, "retired_")) {
      state.retired.emplace_back(suffix.substr(8));
      expected = "1";
    } else if (suffix == "setup" || matches_hex_suffix(suffix, "hold_")) {
      state.holds.emplace_back(suffix == "setup" ? suffix : suffix.substr(5));
      expected = "0";
    } else if (bound_suffix(suffix)) {
      const std::int64_t value = epoch_digits(suffix.substr(4));
      const bool minimum = starts_with(suffix, "min_");
      (minimum ? state.minimums : state.maximums).push_back(value);
      expected = std::string(EPOCH_COLUMN) + (minimum ? ">=" : "<=") + std::to_string(value);
    } else {
      throw MigrationRefused("unrecognized constraint in the reserved write-fence namespace");
    }
    if (normalised != expected) {
      throw MigrationRefused("write fence constraint " + name + " has an unexpected predicate");
    }
  }
  if (owners.size() > 1) {
    throw MigrationRefused("the table carries write fences from more than one project");
  }
  if (owners.empty() && (!state.minimums.empty() || !state.maximums.empty() ||
                         !state.holds.empty() || !state.retired.empty())) {
    throw MigrationRefused("write fence constraints have no project owner");
  }
  state.identity = metadata.identity;
  if (!owners.empty()) state.project_id = owners.front();
  state.column = metadata.column;
  std::sort(state.minimums.begin(), state.minimums.end());
  std::sort(state.maximums.begin(), state.maximums.end());
  std::sort(state.holds.begin(), state.holds.end());
  std::sort(state.retired.begin(), state.retired.end());
  return state;
}

WriteFence::WriteFence(FenceBackend& backend, std::string table, std::string project_id)
    : backend_(&backend), table_(std::move(table)), project_id_(std::move(project_id)) {
  identity(project_id_, "project_id");
  if (table_.empty() || table_.find('\0') != std::string::npos) {
    throw MigrationRefused("write fence table must be a nonempty identifier");
  }
  if (table_ == DRAIN_TABLE || table_ == BACKFILL_TABLE || table_ == WATERMARK_TABLE) {
    throw MigrationRefused("write fences cannot take over an SDK metadata table");
  }
}

FenceState WriteFence::state() {
  FenceState state = fence_state(backend_->metadata(table_));
  if (state.project_id && *state.project_id != project_id_) {
    throw MigrationRefused("the write fence belongs to another project");
  }
  if (state.column == ColumnState::conflict) {
    throw MigrationRefused("the reserved write-epoch column has an incompatible definition");
  }
  return state;
}

FenceState WriteFence::ready() {
  FenceState current = state();
  if (!current.complete()) throw MigrationRefused("the write fence is not fully provisioned");
  return current;
}

FenceState WriteFence::prepare(std::int64_t epoch) {
  epoch = check_epoch(epoch);
  const FenceState current = state();
  const bool setting_up =
      std::find(current.holds.begin(), current.holds.end(), "setup") != current.holds.end();
  if (current.complete() && !setting_up) {
    if (current.epoch() != epoch) {
      throw MigrationRefused("provisioning cannot change an existing write epoch");
    }
    return current;
  }
  if (!current.project_id && current.column != ColumnState::absent) {
    throw MigrationRefused("the reserved write-epoch column is not owned by this project");
  }
  if (current.lower_epoch() && *current.lower_epoch() > epoch) {
    throw MigrationRefused("a write epoch cannot move backwards");
  }
  if (!current.project_id) {
    backend_->add_constraint(table_, std::string(FENCE_PREFIX) + "owner_" + project_id_, "1");
  }
  backend_->add_constraint(table_, std::string(FENCE_SETUP), "0");
  backend_->add_column(table_);
  bounds(epoch);
  backend_->drain(table_, project_id_, "setup");
  backend_->drop_constraint(table_, std::string(FENCE_SETUP));
  return ready();
}

FenceState WriteFence::freeze(const std::string& request_id) {
  identity(request_id, "request_id");
  const FenceState current = ready();
  if (std::find(current.retired.begin(), current.retired.end(), request_id) !=
      current.retired.end()) {
    throw MigrationRefused("a completed write barrier id cannot be reused");
  }
  backend_->add_constraint(table_, std::string(FENCE_PREFIX) + "hold_" + request_id, "0");
  // Always drain, even on a retry: the constraint's existence proves admission is closed, not that
  // an INSERT which captured older metadata has finished.
  backend_->drain(table_, project_id_, request_id);
  return ready();
}

FenceState WriteFence::resume(const std::string& request_id) {
  identity(request_id, "request_id");
  backend_->restore(table_, project_id_, request_id);
  return freeze(request_id);
}

FenceState WriteFence::resume_prepare(std::int64_t epoch) {
  epoch = check_epoch(epoch);
  backend_->restore(table_, project_id_, "setup");
  return prepare(epoch);
}

FenceState WriteFence::advance(std::int64_t epoch) {
  epoch = check_epoch(epoch);
  const FenceState current = ready();
  if (current.holds.empty()) {
    throw MigrationRefused("changing a write epoch needs a named write barrier");
  }
  if (current.lower_epoch() && epoch < *current.lower_epoch()) {
    throw MigrationRefused("a write epoch cannot move backwards");
  }
  bounds(epoch);
  return ready();
}

void WriteFence::bounds(std::int64_t epoch) {
  const std::string number = std::to_string(epoch);
  backend_->add_constraint(table_, std::string(FENCE_PREFIX) + "min_" + number,
                           std::string(EPOCH_COLUMN) + " >= " + number);
  backend_->add_constraint(table_, std::string(FENCE_PREFIX) + "max_" + number,
                           std::string(EPOCH_COLUMN) + " <= " + number);
  // Remove only bounds weaker or older than the one just installed, by their exact names: a stale
  // caller must never remove a newer constraint discovered after its earlier check.
  const FenceState current = state();
  if (current.lower_epoch() && *current.lower_epoch() > epoch) {
    throw MigrationRefused("another executor installed a newer write epoch; table stays closed");
  }
  for (const std::int64_t old : current.maximums) {
    if (old < epoch) {
      backend_->drop_constraint(table_, std::string(FENCE_PREFIX) + "max_" + std::to_string(old));
    }
  }
  for (const std::int64_t old : current.minimums) {
    if (old < epoch) {
      backend_->drop_constraint(table_, std::string(FENCE_PREFIX) + "min_" + std::to_string(old));
    }
  }
}

FenceState WriteFence::release(const std::string& request_id) {
  identity(request_id, "request_id");
  const FenceState current = ready();
  if (!current.epoch()) {
    throw MigrationRefused("cannot release a barrier with an incomplete epoch change");
  }
  if (std::find(current.holds.begin(), current.holds.end(), request_id) == current.holds.end()) {
    return current;
  }
  // Completion is recorded before admission is released: a delayed invocation must never recreate a
  // completed hold, nor reuse its id for a later generation and inherit an old release.
  backend_->add_constraint(table_, std::string(FENCE_PREFIX) + "retired_" + request_id, "1");
  backend_->drop_constraint(table_, std::string(FENCE_PREFIX) + "hold_" + request_id);
  return ready();
}

}  // namespace sde
