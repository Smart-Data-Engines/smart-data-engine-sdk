"""Declaration, type mapping, IR shape and versioning."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

import pytest

import sde
from sde.errors import DeclarationError


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _simple() -> tuple[type, type]:
    @sde.entity
    class User:
        id: uuid.UUID
        email: str

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        total: Annotated[decimal.Decimal, sde.precision(12, 2)]
        created_at: dt.datetime

    return User, Order


def test_fields_and_relations_are_separated() -> None:
    user, order = _simple()
    model = sde.build_model(user, order)
    assert [f.name for f in model.entity("Order").fields] == ["created_at", "id", "total"]
    assert [(r.source, r.name, r.target) for r in model.relations] == [("Order", "user", "User")]


def test_type_mapping() -> None:
    @sde.entity
    class Everything:
        id: uuid.UUID
        flag: bool
        small: sde.Int32
        big: int
        approx: float
        narrow: sde.Float32
        money: Annotated[decimal.Decimal, sde.precision(19, 4)]
        text: str
        blob: bytes
        day: dt.date
        moment: dt.datetime
        naive: sde.Timestamp
        doc: sde.Json
        maybe: str | None

    model = sde.build_model(Everything)
    got = {f.name: (f.type, f.nullable) for f in model.entity("Everything").fields}
    assert got == {
        "id": ("uuid", False),
        "flag": ("bool", False),
        "small": ("int32", False),
        "big": ("int64", False),
        "approx": ("float64", False),
        "narrow": ("float32", False),
        "money": ("decimal(19,4)", False),
        "text": ("string", False),
        "blob": ("bytes", False),
        "day": ("date", False),
        # datetime maps to timestamptz on purpose; a naive timestamp has to be asked for.
        "moment": ("timestamptz", False),
        "naive": ("timestamp", False),
        "doc": ("json", False),
        "maybe": ("string", True),
    }


def test_decimal_without_precision_is_refused() -> None:
    @sde.entity
    class Bad:
        id: uuid.UUID
        amount: decimal.Decimal

    with pytest.raises(DeclarationError, match="Decimal without precision"):
        sde.build_model(Bad)


def test_unmapped_type_is_refused_rather_than_guessed() -> None:
    class Custom:
        pass

    @sde.entity
    class Bad:
        id: uuid.UUID
        thing: Custom

    with pytest.raises(DeclarationError, match="neutral type vocabulary"):
        sde.build_model(Bad)


def test_union_of_two_real_types_is_refused() -> None:
    @sde.entity
    class Bad:
        id: uuid.UUID
        either: int | str

    with pytest.raises(DeclarationError, match="union of several types"):
        sde.build_model(Bad)


def test_version_does_not_depend_on_declaration_order() -> None:
    user, order = _simple()
    assert sde.build_model(user, order).version == sde.build_model(order, user).version


def test_version_changes_when_the_model_changes() -> None:
    user, order = _simple()
    before = sde.build_model(user, order).version

    sde.clear_registry()

    @sde.entity
    class User2:
        id: uuid.UUID
        email: str
        # one more field, and nothing else different
        name: str

        class Meta:
            pii = ["email"]

    after = sde.build_model(User2).version
    assert before != after


def test_key_is_positional_in_the_ir_not_by_array_order() -> None:
    @sde.entity
    class Tenanted:
        tenant: uuid.UUID
        id: uuid.UUID
        payload: str

        class Meta:
            key = ["tenant", "id"]

    model = sde.build_model(Tenanted)
    key_ir = model.ir["entities"][0]["key"]
    assert key_ir == [{"field": "tenant", "position": 0}, {"field": "id", "position": 1}]

    # And the order is load-bearing: swapping it is a different model.
    sde.clear_registry()

    @sde.entity
    class Tenanted2:
        tenant: uuid.UUID
        id: uuid.UUID
        payload: str

        class Meta:
            key = ["id", "tenant"]

    assert sde.build_model(Tenanted2).version != model.version


def test_missing_key_is_refused() -> None:
    @sde.entity
    class NoKey:
        name: str

    with pytest.raises(DeclarationError, match=r"no 'id' field and no Meta.key"):
        sde.build_model(NoKey)


def test_relation_to_undeclared_entity_is_refused() -> None:
    class Ghost:
        pass

    @sde.entity
    class Order:
        id: uuid.UUID
        ghost: sde.Ref[Ghost]  # type: ignore[type-var]

    with pytest.raises(DeclarationError, match="not a declared entity"):
        sde.build_model(Order)


def test_pii_must_name_real_fields() -> None:
    @sde.entity
    class User:
        id: uuid.UUID
        email: str

        class Meta:
            pii = ["nickname"]

    with pytest.raises(DeclarationError, match=r"Meta.pii names"):
        sde.build_model(User)


def test_unknown_meta_key_is_refused() -> None:
    with pytest.raises(DeclarationError, match="not one of the four invariants"):

        @sde.entity
        class Bad:
            id: uuid.UUID

            class Meta:
                shard_by = "id"


def test_atomic_is_symmetric_and_transitive() -> None:
    @sde.entity
    class A:
        id: uuid.UUID
        v: str

    @sde.entity
    class B:
        id: uuid.UUID
        v: str

        class Meta:
            atomic_with = ["A"]

    @sde.entity
    class C:
        id: uuid.UUID
        v: str

        class Meta:
            atomic_with = ["B"]

    model = sde.build_model(A, B, C)
    # Declared once, on one side, in a chain - and it still merges into one group, because anything
    # else could not be implemented on a single engine's transaction.
    assert model.atomic == (("A", "B", "C"),)


def test_atomic_with_itself_is_refused() -> None:
    @sde.entity
    class A:
        id: uuid.UUID
        v: str

        class Meta:
            atomic_with = ["A"]

    with pytest.raises(DeclarationError, match="names itself"):
        sde.build_model(A)


def test_cost_ceiling_is_a_decimal_string() -> None:
    user, order = _simple()
    model = sde.build_model(user, order, cost_ceiling={"amount": "500.00", "currency": "EUR"})
    assert model.ir["cost_ceiling"] == {"amount": "500.00", "currency": "EUR"}
    # And it is part of the model's identity, because it changes what the planner may choose.
    assert model.version != sde.build_model(user, order).version


def test_entity_without_fields_is_refused() -> None:
    @sde.entity
    class User:
        id: uuid.UUID
        name: str

    @sde.entity
    class Membership:
        user: sde.Ref[User]

    with pytest.raises(DeclarationError, match="no fields, only relations"):
        sde.build_model(User, Membership)


def test_building_from_the_registry_needs_no_arguments() -> None:
    user, order = _simple()
    explicit = sde.build_model(user, order)
    implicit = sde.build_model()
    assert explicit.version == implicit.version
