"""The wide table for reading, and the field it will not copy.

Two requirements meet here. 5.1 says one logical model may be materialised differently in different
engines - normalised in a transactional store to write, flattened in an analytical one to read. 5.5
says a field declared as personal data is **not** denormalised into an analytical materialisation
unless the client allowed it for that field.

The second is the reason this derivation is in the library rather than in the planner. An analytical
materialisation is a second copy of the data in a different engine with different access controls,
so "we do not copy your personal data there" is a promise worth being able to *read* rather than
take on trust - and the library is the half a client can read.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

import pytest

import sde
from sde.errors import DeclarationError


def _model() -> sde.LogicalModel:
    """Orders and users in one group, because a `Ref` colocates.

    `User.email` and `User.full_name` are personal data; `Order.note` is too, and it is on the root
    - which matters, because the rule has no exception for the root and a test with personal data
    only on the inlined side would not show that.
    """
    sde.clear_registry()

    @sde.entity
    class User:
        id: uuid.UUID
        email: str
        full_name: str
        country: str

        class Meta:
            pii = ["email", "full_name"]

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        total: Annotated[decimal.Decimal, sde.precision(12, 2)]
        placed_at: dt.datetime
        note: str

        class Meta:
            pii = ["note"]

    return sde.build_model(User, Order)


def _group(model: sde.LogicalModel) -> sde.Group:
    return sde.colocation_groups(model)[0]


def _derive(**kwargs: object) -> sde.DerivedLayout:
    model = _model()
    return sde.denormalized_layout(model, _group(model), **kwargs)  # type: ignore[arg-type]


# ── 5.1 One model, two shapes ───────────────────────────────────────────────────────────────────


def test_the_wide_table_has_one_grain_and_inlines_the_target() -> None:
    derived = _derive()
    assert derived.root == "Order", "one row per order; User is the side that gets flattened in"
    assert derived.layout.tables == {"Order": "order_wide"}
    assert derived.inlined == (("user", "User"),)

    columns = derived.layout.columns["Order"]
    assert "total" in columns and "placed_at" in columns
    assert "user_country" in columns, "an inlined column keeps the relation name as its prefix"


def test_the_inlined_names_reuse_the_foreign_key_convention() -> None:
    """`<relation>_<field>`, the same shape the normalised layout already uses for the key.

    A second convention would mean a client reading their own analytical table has to look at a map
    to know where a column came from.
    """
    normalised = sde.default_layout(_model(), _group(_model()), dialect="postgres")
    sde.clear_registry()
    model = _model()
    derived = sde.denormalized_layout(model, _group(model))

    assert "user_id" in normalised.columns["Order"], "the foreign key is named this way"
    assert "user_country" in derived.layout.columns["Order"]


def test_the_foreign_key_is_dropped_because_the_key_arrives_inlined() -> None:
    """Keeping both would put one value in two columns of one row.

    And then a reader has to decide which is authoritative, for a table where the answer is "they
    are the same by construction, until one day they are not".
    """
    derived = _derive()
    columns = derived.layout.columns["Order"]
    assert "user_id" in columns, "arrives as the inlined User.id"
    assert sum(1 for name in columns if name.endswith("_id") and name.startswith("user")) == 1


def test_the_wide_table_is_typed_for_the_engine_it_is_going_to() -> None:
    ch = _derive(dialect="clickhouse").layout.columns["Order"]
    sde.clear_registry()
    pg = _derive(dialect="postgres").layout.columns["Order"]
    assert ch["total"] == "Decimal(12, 2)"
    assert pg["total"] == "numeric(12,2)"


# ── 5.5 The negative test ───────────────────────────────────────────────────────────────────────


def test_no_personal_data_reaches_the_wide_table_without_an_allowance() -> None:
    """The test this task exists for, and it asserts absence rather than presence.

    Fails closed: an empty allowance excludes every declared personal-data field, on the root as
    well as on the inlined side. A rule with an exception for the root would be a rule nobody could
    state in one sentence, and the copy arriving in another engine is the thing 5.5 is about - not
    which side of a join it came from.
    """
    derived = _derive()
    columns = derived.layout.columns["Order"]

    assert "note" not in columns, "the root's own personal data is not copied either"
    assert "user_email" not in columns
    assert "user_full_name" not in columns
    assert derived.excluded_pii == ("Order.note", "User.email", "User.full_name")

    # And the fields that are not personal data are all still there, or the rule would be a hammer.
    assert {"id", "total", "placed_at", "user_id", "user_country"} <= set(columns)


def test_an_allowance_is_per_field_and_only_that_field_arrives() -> None:
    derived = _derive(include_pii=["User.email"])
    columns = derived.layout.columns["Order"]
    assert "user_email" in columns
    assert "user_full_name" not in columns
    assert "note" not in columns
    assert derived.excluded_pii == ("Order.note", "User.full_name")


def test_what_was_excluded_is_reported_rather_than_silently_absent() -> None:
    """An analyst finding no email has to be able to tell a decision from missing data.

    And the client has to be able to see which fields they would have to allow explicitly, which is
    a different list from "all the personal data in the model".
    """
    assert _derive().excluded_pii == ("Order.note", "User.email", "User.full_name")
    sde.clear_registry()
    assert _derive(include_pii=["Order.note", "User.email", "User.full_name"]).excluded_pii == ()


def test_an_allowance_naming_something_unknown_is_refused_in_both_directions() -> None:
    """A misspelling would leave the field excluded while the client believed it was allowed."""
    with pytest.raises(DeclarationError, match=r"include_pii names \['User.e-mail'\]"):
        _derive(include_pii=["User.e-mail"])
    sde.clear_registry()
    with pytest.raises(DeclarationError, match=r"include_pii names \['User.country'\]"):
        _derive(include_pii=["User.country"])


def test_the_refusal_lists_what_this_group_does_declare() -> None:
    with pytest.raises(DeclarationError) as raised:
        _derive(include_pii=["nope"])
    for name in ("Order.note", "User.email", "User.full_name"):
        assert name in str(raised.value)


# ── The four refusals ───────────────────────────────────────────────────────────────────────────


def test_a_group_with_nothing_to_flatten_is_refused() -> None:
    """The wide table would be the default layout under another name.

    A second copy paying for storage, replication and lag to answer the questions the source already
    answers - which is the same failure as a derived copy nothing routes to, one step earlier.
    """
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: uuid.UUID
        celsius: Annotated[decimal.Decimal, sde.precision(6, 2)]

    model = sde.build_model(Reading)
    with pytest.raises(DeclarationError, match="nothing to denormalise"):
        sde.denormalized_layout(model, _group(model))


def test_an_ambiguous_root_is_refused_rather_than_guessed() -> None:
    """One row of the wide table would mean whatever the function chose."""
    sde.clear_registry()

    @sde.entity
    class User:
        id: uuid.UUID

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]

    @sde.entity
    class Invoice:
        id: uuid.UUID
        user: sde.Ref[User]

    model = sde.build_model(User, Order, Invoice)
    with pytest.raises(DeclarationError, match="candidate roots"):
        sde.denormalized_layout(model, _group(model))


def test_a_fixed_schema_engine_has_no_wide_table_to_make() -> None:
    model = _model()
    with pytest.raises(DeclarationError, match="imposes its own schema"):
        sde.denormalized_layout(model, _group(model), dialect="orderbook")


def test_the_wide_layout_renders_to_ddl_like_any_other() -> None:
    """Because the point of one derivation is that everything downstream stays the same."""
    derived = _derive(dialect="clickhouse")
    statements = sde.schema_statements(
        derived.layout, keys={"Order": ("id",)}, dialect="clickhouse"
    )
    assert len(statements) == 1
    assert "order_wide" in statements[0]
    assert "email" not in statements[0], "the DDL cannot create a column the layout does not have"


def test_a_target_whose_key_is_personal_data_does_not_arrive_as_a_foreign_key() -> None:
    """The leak a surviving mutant found, and the reason the root pass defers to the inlined one.

    A foreign-key column is named after the relation - `person_national_id` - not after a field of
    the root, so the personal-data check does not recognise it: the name is not in the root's
    declared fields and the root's own list is what that check consults. So a target whose *key* is
    personal data would arrive in the wide table through the foreign key, with the inlined pass
    dutifully excluding the very same value one loop later.

    The comment at that branch first claimed it was about duplicate columns. It is not - the foreign
    key and the inlined key produce the same column name by construction, so the assignment is
    idempotent either way. Testing what it actually prevents was the only way to find that out.
    """
    sde.clear_registry()

    @sde.entity
    class Person:
        national_id: str
        country: str

        class Meta:
            key = ["national_id"]
            pii = ["national_id"]

    @sde.entity
    class Visit:
        id: uuid.UUID
        person: sde.Ref[Person]

    model = sde.build_model(Person, Visit)
    derived = sde.denormalized_layout(model, _group(model))

    columns = derived.layout.columns[derived.root]
    assert derived.root == "Visit"
    assert "person_national_id" not in columns, (
        "the target's key is personal data and it reached the wide table through the foreign key"
    )
    assert "person_country" in columns
    assert derived.excluded_pii == ("Person.national_id",)


def test_a_personal_data_key_can_still_be_allowed_explicitly() -> None:
    """Because the rule is an allowance, not a prohibition - and a wide table with no join key is
    often useless, so this is the case a client will actually reach for."""
    sde.clear_registry()

    @sde.entity
    class Person:
        national_id: str
        country: str

        class Meta:
            key = ["national_id"]
            pii = ["national_id"]

    @sde.entity
    class Visit:
        id: uuid.UUID
        person: sde.Ref[Person]

    model = sde.build_model(Person, Visit)
    derived = sde.denormalized_layout(
        model, _group(model), include_pii=["Person.national_id"]
    )
    assert "person_national_id" in derived.layout.columns[derived.root]
    assert derived.excluded_pii == ()
