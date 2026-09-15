from __future__ import annotations

import pytest
from tests.plugins.support import candidate, registration

from chassis.core.errors import ConfigurationError
from chassis.plugins.resolver import DependencyResolver


def test_plan_is_deterministic_regardless_of_input_order() -> None:
    database = candidate("db", provides={"database": "1.0.0"})
    memory = candidate("memory", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"})
    agent = candidate("agent", requires={"memory": ">=1,<2"})

    resolver = DependencyResolver()
    first = resolver.resolve([agent, memory, database])
    second = resolver.resolve([database, agent, memory])

    assert first == second
    assert first.activation_order == ("db", "memory", "agent")
    assert first.edges == (("db", "memory"), ("memory", "agent"))


def test_optional_requirement_does_not_block_activation() -> None:
    resolver = DependencyResolver()
    plan = resolver.resolve(
        [candidate("optional-consumer", requires={}, optional={"database": ">=1,<2"})]
    )

    assert plan.activation_order == ("optional-consumer",)
    entry = plan.plan_for("optional-consumer")
    assert entry is not None
    assert entry.requirements[0].requirement.optional is True
    assert entry.requirements[0].status == "no_provider"
    assert entry.reasons == ()


def test_shared_provider_is_ordered_before_all_consumers() -> None:
    plan = DependencyResolver().resolve(
        [
            candidate("shared", provides={"database": "1.0.0"}),
            candidate("b", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"}),
            candidate("c", provides={"cache": "1.0.0"}, requires={"database": ">=1,<2"}),
            candidate("consumer", requires={"cache": ">=1,<2"}),
        ]
    )

    assert plan.activation_order == ("shared", "b", "c", "consumer")


def test_provider_preference_is_scoped_per_consumer_then_global() -> None:
    provider_a = candidate("db-a", provides={"database": "1.0.0"})
    provider_b = candidate("db-b", provides={"database": "1.0.0"})
    consumer = candidate("consumer", requires={"database": ">=1,<2"})
    resolver = DependencyResolver()

    assert resolver.resolve([provider_a, provider_b, consumer]).pending == ("consumer",)

    scoped = resolver.resolve(
        [provider_a, provider_b, consumer], prefer={"consumer:database": "db-b"}
    )
    assert scoped.activation_order == ("db-a", "db-b", "consumer")
    plan = scoped.plan_for("consumer")
    assert plan is not None
    assert plan.requirements[0].provider_entry_id == "db-b"

    global_preference = resolver.resolve(
        [provider_a, provider_b, consumer], prefer={"database": "db-a"}
    )
    assert global_preference.plan_for("consumer") is not None
    assert global_preference.plan_for("consumer").requirements[0].provider_entry_id == "db-a"  # type: ignore[union-attr]


def test_removing_a_provider_cascades_to_its_consumers() -> None:
    plan = DependencyResolver().resolve(
        [
            candidate("memory", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"}),
            candidate("agent", requires={"memory": ">=1,<2"}),
        ]
    )

    assert plan.activation_order == ()
    assert plan.pending == ("agent", "memory")
    assert plan.plan_for("memory").requirements[0].status == "no_provider"  # type: ignore[union-attr]
    assert "no provider for 'database'" in plan.explain("memory")


def test_live_registrations_are_authoritative_for_active_instances() -> None:
    # The manifest claims a database, but the active instance registered nothing.
    liar = candidate("liar", provides={"database": "1.0.0"}, active=True, instance_id="i-1")
    consumer = candidate("consumer", requires={"database": ">=1,<2"})

    plan = DependencyResolver().resolve([liar, consumer])

    assert plan.pending == ("consumer",)


def test_active_registration_satisfies_a_consumer() -> None:
    live = candidate(
        "live",
        provides={"database": "1.0.0"},
        active=True,
        instance_id="i-1",
        registrations=(registration("i-1", "postgres", "database", "1.2.0"),),
    )
    consumer = candidate("consumer", requires={"database": ">=1,<2"})

    plan = DependencyResolver().resolve([live, consumer])

    assert plan.activation_order == ("live", "consumer")
    assert plan.plan_for("consumer").requirements[0].provider_instance_id == "i-1"  # type: ignore[union-attr]


def test_three_node_cycle_is_detected_as_one_component() -> None:
    a = candidate("a", provides={"x": "1.0.0"}, requires={"z": ">=1,<2"})
    b = candidate("b", provides={"y": "1.0.0"}, requires={"x": ">=1,<2"})
    c = candidate("c", provides={"z": "1.0.0"}, requires={"y": ">=1,<2"})
    outsider = candidate("d", provides={"w": "1.0.0"})

    plan = DependencyResolver().resolve([a, b, c, outsider])

    assert plan.cycles == (("a", "b", "c"),)
    assert plan.activation_order == ("d",)
    assert plan.pending == ("a", "b", "c")
    assert plan.plan_for("a").reasons == ("dependency cycle",)  # type: ignore[union-attr]


def test_duplicate_entry_ids_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        DependencyResolver().resolve(
            [candidate("dup", provides={"x": "1.0.0"}), candidate("dup", provides={"y": "1.0.0"})]
        )


def test_requirement_of_a_failed_provider_cascades() -> None:
    # `memory` is desired and declared, but its own provider is gone, so nothing
    # that depends on it may activate either.
    plan = DependencyResolver().resolve(
        [
            candidate("memory", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"}),
            candidate("agent", requires={"memory": ">=1,<2"}),
            candidate("tools", requires={"memory": ">=1,<2"}),
        ]
    )

    assert plan.activation_order == ()
    assert plan.pending == ("agent", "memory", "tools")


def test_plan_to_dict_is_serializable() -> None:
    plan = DependencyResolver().resolve([candidate("db", provides={"database": "1.0.0"})])
    payload = plan.to_dict()

    assert payload["activation_order"] == ["db"]
    assert payload["plugins"][0]["plugin"] == "db@1.0.0"
