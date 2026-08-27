"""Hashed identifiers, and the invariance that makes hashing worth offering.

The test that matters here is not that hashing produces different names - that is arithmetic. It is
that hashing does not produce a different *decision*. Requirement 11.4 forbids any scoring feature
from reading the meaning of a name, and this file is how that ban is enforced: the moment somebody
writes ``if "log" in table_name`` in the decision path, the invariance test fails.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from pathlib import Path

import pytest

import sde
from sde.errors import DeclarationError
from sde.hashing import NameMap, hash_identifiers, load_or_create_salt
from sde.telemetry import has_time_dimension

SALT = b"a" * 32


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _shop() -> sde.LogicalModel:
    """Everything hashing has to survive: relations, atomicity, pii, a composite key."""

    @sde.entity
    class User:
        id: uuid.UUID
        email: str

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        tenant: uuid.UUID
        id: uuid.UUID
        user: sde.Ref[User]
        placed_at: dt.datetime

        class Meta:
            key = ["tenant", "id"]
            residency = "EU"

    @sde.entity
    class Payment:
        id: uuid.UUID
        amount: int

        class Meta:
            atomic_with = ["Order"]

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime

    return sde.build_model(User, Order, Payment, Event)


# --- the invariance ----------------------------------------------------------------------------


def test_hashing_does_not_change_how_entities_are_grouped() -> None:
    """The partition is identical. This is the assertion the whole feature rests on.

    Note what is compared: the *partition*, translated through the name map - not the group names.
    Group names cannot match, because a group is named after its alphabetically first member and
    hashing changes which member that is. Comparing names would either fail for the wrong reason or
    force the implementation to keep real names around, which would defeat the point.
    """
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)

    original = {frozenset(g.members) for g in sde.colocation_groups(model)}
    translated = {
        frozenset(names.entity(member) for member in g.members)
        for g in sde.colocation_groups(model)
    }
    after = {frozenset(g.members) for g in sde.colocation_groups(hashed)}

    assert translated == after, (
        "hashing changed the colocation partition. Something in the graph construction is "
        "reading a name rather than a relation or a declared atomicity."
    )
    # Two groups: Order pulls in User through the relation and Payment through the declared
    # atomicity, while Event references nothing and stands alone. That split is the product's
    # thesis, so it is asserted rather than assumed - if hashing ever merged or divided a group, the
    # counts would move even when the frozensets happened to line up.
    assert len(original) == len(after) == 2


def test_hashing_does_not_change_the_shapes_the_model_admits() -> None:
    # Identifiers differ, so shape ids differ. What must not differ is the multiset of operations
    # the model admits: same kinds, same arities, same distribution across groups.
    model = _shop()
    hashed, _ = hash_identifiers(model, SALT)

    def profile(m: sde.LogicalModel) -> dict[tuple[str, int], int]:
        counts: dict[tuple[str, int], int] = {}
        for shape in sde.enumerate_shapes(m):
            key = (shape.kind, len(shape.fields))
            counts[key] = counts.get(key, 0) + 1
        return counts

    assert profile(model) == profile(hashed)


def test_hashing_does_not_change_a_time_dimension() -> None:
    # The derivation reads types, so it has to be invariant - and this is the test that would have
    # caught it if it read names, which was the first thing it was tempting to do.
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    for group in sde.colocation_groups(model):
        translated = frozenset(names.entity(m) for m in group.members)
        after = next(
            g for g in sde.colocation_groups(hashed) if frozenset(g.members) == translated
        )
        assert has_time_dimension(model, group) == has_time_dimension(hashed, after)


def test_hashing_does_not_change_column_types() -> None:
    # The physical layout is derived from types; only the names should move.
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)

    group = next(g for g in sde.colocation_groups(model) if "Order" in g.members)
    after = next(
        g
        for g in sde.colocation_groups(hashed)
        if frozenset(g.members) == frozenset(names.entity(m) for m in group.members)
    )

    before_layout = sde.default_layout(model, group)
    after_layout = sde.default_layout(hashed, after)

    before_types = sorted(before_layout.columns["Order"].values())
    after_types = sorted(after_layout.columns[names.entity("Order")].values())
    assert before_types == after_types


# --- what hashing must and must not touch -------------------------------------------------------


def test_names_are_replaced_everywhere_they_appear() -> None:
    model = _shop()
    hashed, _ = hash_identifiers(model, SALT)
    ir = str(hashed.ir)
    for leaked in ("User", "Order", "Payment", "Event", "email", "placed_at", "tenant"):
        assert leaked not in ir, f"{leaked} survived hashing and is in the IR"


def test_the_hashed_ir_does_not_encode_the_order_of_the_real_names() -> None:
    """The leak the TypeScript port found, and the reason a second implementation is worth its cost.

    Hashing rebuilds each entity with digests for names. The first version kept the original
    sequence, so the field arrays in the hashed IR came out ordered by the *real* field names -
    alphabetically, which is a small amount of exactly the information hashing exists to hide, and
    which no library that had never seen those names could reproduce. Python sorted nowhere,
    TypeScript sorted in ``assemble``, both passed their own suites, and the shared vector is what
    made them disagree out loud.

    The assertion is deliberately about the *hashed* names being in order rather than about any
    particular sequence: it holds whatever the salt is, and it fails the moment somebody
    reintroduces "keep the caller's order".
    """
    model = _shop()
    hashed, _ = hash_identifiers(model, SALT)

    for entity in hashed.entities:
        names = [f["name"] for f in _fields_in_ir(hashed, entity.name)]
        assert names == sorted(names), (
            f"{entity.name}: fields in the IR are not in order of their hashed names, so their "
            "sequence still carries information about the names they replaced."
        )

    # And the same claim from the other side: reordering the declaration must not change the bytes.
    sde.clear_registry()
    reversed_declaration = _shop_reversed()
    other, _ = hash_identifiers(reversed_declaration, SALT)
    assert other.version == hashed.version, (
        "declaring the same entities in a different order produced different hashed bytes. Array "
        "order in the IR has to be derived from the IR's own names, not from the caller."
    )


def _fields_in_ir(model: sde.LogicalModel, entity: str) -> list[dict[str, object]]:
    for spec in model.ir["entities"]:
        if spec["name"] == entity:
            fields: list[dict[str, object]] = spec["fields"]
            return fields
    raise AssertionError(f"{entity} is not in the IR")


def _shop_reversed() -> sde.LogicalModel:
    """The same model, declared in the opposite order, with fields reversed too."""

    @sde.entity
    class Event:
        at: dt.datetime
        name: str
        id: uuid.UUID

    @sde.entity
    class User:
        email: str
        id: uuid.UUID

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        placed_at: dt.datetime
        user: sde.Ref[User]
        id: uuid.UUID
        tenant: uuid.UUID

        class Meta:
            key = ["tenant", "id"]
            residency = "EU"

    @sde.entity
    class Payment:
        amount: int
        id: uuid.UUID

        class Meta:
            atomic_with = ["Order"]

    return sde.build_model(Event, User, Order, Payment)


def test_residency_survives_because_it_is_not_an_identifier() -> None:
    # Hashing a jurisdiction would make a hard placement constraint unenforceable, which is a worse
    # outcome than us knowing that some group must stay in the EU.
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    assert hashed.entity(names.entity("Order")).residency == "EU"


def test_the_key_still_points_at_fields_and_keeps_its_order() -> None:
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    spec = hashed.entity(names.entity("Order"))
    assert spec.key == (names.field("Order", "tenant"), names.field("Order", "id"))
    assert set(spec.key) <= {f.name for f in spec.fields}


def test_pii_still_points_at_fields() -> None:
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    spec = hashed.entity(names.entity("User"))
    assert spec.pii == (names.field("User", "email"),)


def test_the_same_field_name_on_two_entities_hashes_differently() -> None:
    # Otherwise we would learn that two entities share a field called `id` without being able to
    # read the name, which is the structural leak hashing exists to close.
    model = _shop()
    _, names = hash_identifiers(model, SALT)
    assert names.field("User", "id") != names.field("Order", "id")


def test_the_same_name_in_two_normal_forms_hashes_the_same() -> None:
    """Found by writing the format contract, not by a test, and it would have shipped.

    The canonical encoder normalises to NFC before emitting bytes, so two libraries that declare
    ``Zamowienie`` in different normal forms compute the same model version. Hashing before
    normalising threw that away: two digests, two versions, and a map issued for the Python service
    that the TypeScript one refuses. Invisible in ASCII, so the first person to hit it would have
    been a client whose entity names are not English.
    """
    nfc = unicodedata.normalize("NFC", "Zam\u00f3wienie")
    nfd = unicodedata.normalize("NFD", "Zam\u00f3wienie")
    assert nfc != nfd, "the two forms are identical, so this test proves nothing"

    def one(name: str) -> sde.LogicalModel:
        sde.clear_registry()
        declared = type(name, (), {"__annotations__": {"id": uuid.UUID}})
        return sde.build_model(sde.entity(declared))

    composed, decomposed = one(nfc), one(nfd)
    assert composed.version == decomposed.version, "the unhashed models already disagree"

    first, _ = hash_identifiers(composed, SALT)
    second, _ = hash_identifiers(decomposed, SALT)
    assert first.version == second.version, (
        "hashing is not normalising before the HMAC. The same identifier written two ways produces "
        "two hashed models, so a placement map issued for one service is refused by the other."
    )


def test_a_different_salt_gives_different_names() -> None:
    model = _shop()
    first, _ = hash_identifiers(model, b"a" * 32)
    second, _ = hash_identifiers(model, b"b" * 32)
    assert first.version != second.version


def test_the_same_salt_is_deterministic() -> None:
    model = _shop()
    first, _ = hash_identifiers(model, SALT)
    sde.clear_registry()
    second, _ = hash_identifiers(_shop(), SALT)
    assert first.version == second.version


# --- the consequence that has to be documented rather than hidden -------------------------------


def test_hashing_is_a_model_change_and_therefore_needs_a_new_map() -> None:
    """Turning hashing on is not a setting, it is a new model.

    A group is named after its alphabetically first member and shape ids include the group and
    entity names, so the canonical IR differs and so does the version. A map issued for the unhashed
    model is refused for the hashed one - which is requirement 1.3 doing its job, and much better
    than a map being half-applied.
    """
    model = _shop()
    hashed, _ = hash_identifiers(model, SALT)
    assert model.version != hashed.version

    raw = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {"source": {"id": f"{g.name}@e", "engine": "e", "layout": {"auto": True}}}
            for g in sde.colocation_groups(model)
        },
    }
    with pytest.raises(sde.MapError, match="is for model version"):
        sde.load_map(raw, model=hashed)


# --- refusals ----------------------------------------------------------------------------------


def test_a_short_salt_is_refused() -> None:
    with pytest.raises(DeclarationError, match="at least 16 bytes"):
        hash_identifiers(_shop(), b"short")


def test_asking_for_a_name_that_is_not_in_the_model() -> None:
    _, names = hash_identifiers(_shop(), SALT)
    with pytest.raises(DeclarationError, match="not in this model"):
        names.entity("Ghost")


def test_a_salt_file_is_created_once_and_kept_private(tmp_path: Path) -> None:
    location = tmp_path / "nested" / "salt"
    first = load_or_create_salt(location)
    assert len(first) >= 32
    assert location.stat().st_mode & 0o077 == 0, (
        "the salt file is readable by somebody other than its owner. A readable salt in a shared "
        "image is the same as no salt."
    )
    assert load_or_create_salt(location) == first


def test_a_salt_is_read_back_exactly_as_written_for_every_leading_byte(tmp_path: Path) -> None:
    """The salt file is bytes, and every one of the 256 possible first bytes has to survive a read.

    Deterministic on purpose. The bug this covers was found by
    :func:`test_a_salt_file_is_created_once_and_kept_private` failing once in about twenty CI runs,
    because that test depends on ``secrets.token_bytes`` happening to produce a salt beginning or
    ending with one of the six byte values ``bytes.strip()`` removes. A test that catches a defect
    5% of the time reads as a flaky test, and a flaky test gets re-run rather than read.
    """
    for first in range(256):
        location = tmp_path / f"salt-{first:03d}"
        written = bytes([first]) + b"\x00" * 30 + bytes([first])
        location.write_bytes(written)
        assert load_or_create_salt(location) == written, (
            f"a salt beginning and ending with byte 0x{first:02x} did not survive a round trip. "
            f"The salt is the HMAC key for every hashed name, so a salt that changes between "
            f"processes changes the model version, and the map issued for one is refused by the "
            f"other."
        )


def test_the_six_whitespace_bytes_are_the_ones_that_used_to_be_eaten(tmp_path: Path) -> None:
    """Name the exact byte values, so a future "be forgiving about newlines" has to face them.

    ``bytes.strip()`` with no argument removes ``b' \t\n\r\x0b\x0c'``. Spelling them out is the
    point: the previous implementation looked like it handled a trailing newline, and in fact
    silently shortened one salt in twenty.
    """
    for byte in b" \t\n\r\x0b\x0c":
        location = tmp_path / f"ws-{byte:03d}"
        written = bytes([byte]) * 4 + b"salt-body-that-is-long-enough!!" + bytes([byte]) * 4
        location.write_bytes(written)
        read = load_or_create_salt(location)
        assert read == written, f"byte 0x{byte:02x} was stripped from the salt"
        assert len(read) == len(written)


def test_a_generated_salt_survives_its_own_round_trip(tmp_path: Path) -> None:
    """The real path, run enough times that the 5% case is a certainty rather than a coin flip.

    Two hundred generated salts: the chance that none of them starts or ends with a stripped byte is
    about 0.99 ** 200, under a tenth of a percent. The deterministic tests above are what pin the
    behaviour; this one exercises the real generate-then-read path, which is the one an application
    takes.
    """
    for i in range(200):
        location = tmp_path / f"gen-{i:03d}"
        generated = load_or_create_salt(location)
        assert load_or_create_salt(location) == generated, (
            f"salt {i} changed between the call that created it and the call that read it: "
            f"{generated!r} became {load_or_create_salt(location)!r}"
        )


def test_a_truncated_salt_file_is_refused_rather_than_used(tmp_path: Path) -> None:
    location = tmp_path / "salt"
    location.write_bytes(b"tiny")
    with pytest.raises(DeclarationError, match="shorter than 16 bytes"):
        load_or_create_salt(location)


def test_the_name_map_is_the_only_thing_that_knows_both_sides() -> None:
    # Not a behaviour test so much as a statement about where the secret lives: the hashed model
    # contains no path back to the real names, and the map is never serialised into it.
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    assert isinstance(names, NameMap)
    assert "User" not in str(hashed.ir)
    assert names.entity("User") in str(hashed.ir)
