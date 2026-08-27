"""Hiding identifier names from us, and from the model, without hiding them from the planner's job.

A client in a regulated environment may not want us to see that they have a table called
``patient_diagnosis``. Requirement 11.3 lets them hash entity, field and relation names with a salt
that never leaves their infrastructure, and requirement 11.4 says the consequence must not be a
worse placement: no scoring feature may read the *meaning* of a name, so a hashed model has to be
placed identically to an unhashed one.

That second requirement is the interesting one. It is enforced by a test rather than by review, and
the test is worth more than a dozen unit tests of the planner, because it fails the moment anyone
writes ``if "log" in table_name`` anywhere in the decision path.

# Two consequences worth knowing before turning this on

**Hashing is a model change.** A group's name is its alphabetically first member, and hashing
changes which member that is; operation shape identifiers include the group and entity names, so
they change too. The canonical IR is different, so ``model_version`` is different, so the placement
map is different. Switching hashing on or off is therefore not a setting - it is a new model that
needs a new map, exactly as requirement 1.3 describes. Saying this plainly is cheaper than having a
client discover it when their existing map is refused.

**Your own tables get opaque names.** The physical layout comes from the map, which is keyed by
hashed names, so a table in the client's own database ends up called ``e_9c1f2a7b3d40``. For a
regulated deployment that is the point. For most deployments it is a real cost, and it should be
weighed rather than accepted by default - which is why this is off unless asked for.

# What is not hidden

Types, cardinalities, latencies, call counts, the shape of the graph. All of that is what the
planner actually reasons from, and none of it is a name. What the model loses is semantic signal:
``orders`` tells a language model something that ``e_9c1f2a7b3d40`` does not, so proposals get
measurably worse (requirement 18.10). The deterministic path is unaffected, which is the whole point
of it not reading names.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import DeclarationError
from .model import EntitySpec, FieldSpec, LogicalModel, RelationSpec, assemble

__all__ = ["NameMap", "hash_identifiers", "load_or_create_salt"]

# Twelve hex characters is 48 bits. Collisions matter here - two entities hashing to one name would
# merge them in the IR - so this is checked rather than assumed: hash_identifiers refuses on
# collision instead of producing a model with one entity where there were two.
DIGEST_CHARS = 12

_ENTITY_PREFIX = "e_"
_FIELD_PREFIX = "f_"
_RELATION_PREFIX = "r_"


def load_or_create_salt(path: Path | None = None) -> bytes:
    """Read the client's salt, generating it on first use.

    The salt never appears in a model, a telemetry window or anything sent to us: it is the whole
    mechanism, and a salt we hold is a mechanism we could reverse. Stored with owner-only
    permissions, because a readable salt in a shared container image is the same as no salt.

    The file is read **verbatim**. An earlier version called ``.strip()`` on it, to be forgiving
    about a trailing newline in a hand-made file, and that was a serious bug rather than a kindness.
    ``bytes.strip()`` removes six byte *values* - space, tab, newline, carriage return, ``\x0b`` and
    ``\x0c`` - and a random 32-byte salt begins or ends with one of them about 5% of the time.

    When it did, the process that generated the salt used all 32 bytes and every process afterwards
    used the stripped remainder. So one client computed **two different model versions** for one
    declared model: the map issued for one is refused by the other, and a map that was accepted
    names different tables. The TypeScript library takes the salt as bytes from its caller and
    strips nothing, so a Python service and a Node service sharing one salt file disagreed too - the
    exact failure the byte contract exists to prevent, arriving through the file rather than the
    encoder.

    A file created with ``echo`` therefore includes its newline, and the salt is those bytes
    including it. That is consistent and checkable. Being forgiving would mean choosing an encoding,
    hex or base64, and that changes what is on disk - which changes every name derived from it.
    """
    location = path or Path.home() / ".sde" / "salt"
    if location.exists():
        salt = location.read_bytes()
        if len(salt) < 16:
            raise DeclarationError(
                f"the salt at {location} is shorter than 16 bytes. A short salt is "
                "guessable, and a guessable salt means the names are not hidden. Delete the file "
                "to have a new one generated - but note that a new salt is a new model version "
                "and needs a new map."
            )
        return salt

    location.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    # Written before the mode is set, then narrowed. os.open with 0o600 would be tighter, and is
    # what this should become if the file ever holds more than a salt.
    location.write_bytes(salt)
    os.chmod(location, 0o600)
    return salt


@dataclass(frozen=True)
class NameMap:
    """The translation between what the client wrote and what we see.

    Held only in the client's process. The library needs it because the placement map is keyed by
    hashed names while the application still says ``session.save("User", ...)`` - so every lookup
    crosses this boundary, and it is the only place that does.
    """

    entities: Mapping[str, str]
    fields: Mapping[str, Mapping[str, str]]
    relations: Mapping[str, Mapping[str, str]]

    def entity(self, name: str) -> str:
        try:
            return self.entities[name]
        except KeyError:
            raise DeclarationError(
                f"{name!r} is not in this model, so it has no hashed name. Either it was never "
                "declared, or the model was rebuilt without it."
            ) from None

    def field(self, entity: str, name: str) -> str:
        return self.fields[entity][name]

    def relation(self, entity: str, name: str) -> str:
        return self.relations[entity][name]


def _digest(salt: bytes, prefix: str, *parts: str) -> str:
    """HMAC over the parts, NFC-normalised and joined by a separator no identifier can contain.

    Two details, both of which a port can get wrong silently, so both are pinned in the format
    contract.

    **NFC first.** The canonical encoder normalises before it emits bytes, which is why two
    libraries that declare ``Zamówienie`` in different normal forms compute the *same* model
    version. Hashing a name before normalising it would throw that away: the same identifier written
    two ways would give two digests, two model versions, and a placement map issued for one service
    that the other refuses. Nothing about that is visible in ASCII, so it would have shipped and
    then failed for a client whose entity names are not English.

    **The separator, not concatenation.** Hashing ``("User", "id")`` as ``"Userid"`` would collide
    with ``("Use", "rid")``. Unlikely is not a guarantee when the consequence is two fields becoming
    one column. U+0000 cannot occur in an identifier, so the join is unambiguous.

    The prefix is deliberately outside the HMAC. It labels the digest for a human reading a table
    name; it carries no secret and adding it to the message would only make the derivation harder to
    reproduce.
    """
    message = "\x00".join(unicodedata.normalize("NFC", part) for part in parts)
    digest = hmac.new(salt, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return prefix + digest[:DIGEST_CHARS]


def hash_identifiers(model: LogicalModel, salt: bytes) -> tuple[LogicalModel, NameMap]:
    """Return an equivalent model with every identifier replaced by a keyed digest.

    Field and relation names are hashed *with their entity* in the input, so the same field name on
    two entities produces two different digests. That is not paranoia: leaving them independent
    would let us learn that two entities share a field called ``email`` even though we cannot read
    the name, which is exactly the kind of structural leak hashing is meant to close.
    """
    if len(salt) < 16:
        raise DeclarationError("the salt must be at least 16 bytes")

    entity_names: dict[str, str] = {}
    for spec in model.entities:
        hashed = _digest(salt, _ENTITY_PREFIX, spec.name)
        if hashed in entity_names.values():
            clash = next(k for k, v in entity_names.items() if v == hashed)
            raise DeclarationError(
                f"{spec.name!r} and {clash!r} hash to the same name. Refused rather than merged: a "
                "model with one entity where there were two would place both in one engine and "
                "write both into one table. Change the salt."
            )
        entity_names[spec.name] = hashed

    field_names: dict[str, dict[str, str]] = {}
    relation_names: dict[str, dict[str, str]] = {}

    entities: list[EntitySpec] = []
    for spec in model.entities:
        mapping: dict[str, str] = {}
        for spec_field in spec.fields:
            mapping[spec_field.name] = _digest(
                salt, _FIELD_PREFIX, spec.name, spec_field.name
            )
        if len(set(mapping.values())) != len(mapping):
            raise DeclarationError(
                f"two fields of {spec.name!r} hash to the same name. Refused rather than merged. "
                "Change the salt."
            )
        field_names[spec.name] = mapping

        entities.append(
            EntitySpec(
                name=entity_names[spec.name],
                fields=tuple(
                    FieldSpec(name=mapping[f.name], type=f.type, nullable=f.nullable)
                    for f in spec.fields
                ),
                key=tuple(mapping[k] for k in spec.key),
                pii=tuple(mapping[p] for p in spec.pii),
                # Residency is not an identifier. It is a jurisdiction, it is a hard constraint on
                # placement, and hashing it would make the constraint unenforceable.
                residency=spec.residency,
            )
        )

    relations: list[RelationSpec] = []
    for relation in model.relations:
        hashed = _digest(salt, _RELATION_PREFIX, relation.source, relation.name)
        relation_names.setdefault(relation.source, {})[relation.name] = hashed
        relations.append(
            RelationSpec(
                name=hashed,
                source=entity_names[relation.source],
                target=entity_names[relation.target],
            )
        )

    atomic = tuple(
        tuple(sorted(entity_names[member] for member in group)) for group in model.atomic
    )

    hashed_model = assemble(
        entities=tuple(entities),
        relations=tuple(relations),
        atomic=tuple(sorted(atomic)),
        # The cost ceiling is a number and a currency, not an identifier.
        cost_ceiling=model.cost_ceiling,
    )
    return hashed_model, NameMap(
        entities=entity_names, fields=field_names, relations=relation_names
    )
