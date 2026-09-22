"""CompositionTree.child() enforces composition-tree ownership (roadmap R012).

The :class:`CompositionScope` constructor rejects a parent scope owned by another
composition tree. ``CompositionTree.child()`` is a public entry point for the
same operation, so it must enforce the identical rule instead of silently
creating the new scope inside the foreign tree.
"""

from __future__ import annotations

import pytest

from chassis import Harness
from chassis.composition import CompositionScope
from chassis.core.errors import ConfigurationError


def test_child_rejects_a_foreign_parent_like_the_constructor() -> None:
    tree_a = Harness(name="a").composition
    tree_b = Harness(name="b").composition
    foreign = tree_a.child("research")

    with pytest.raises(ConfigurationError) as from_constructor:
        CompositionScope(tree_b, "mine", parent=foreign)
    with pytest.raises(ConfigurationError) as from_child:
        tree_b.child("mine", parent=foreign)

    assert from_child.value.message == from_constructor.value.message
    assert from_child.value.context == from_constructor.value.context
    assert from_child.value.context["parent"] == "/research"


def test_rejected_foreign_parent_leaves_both_trees_unchanged() -> None:
    tree_a = Harness(name="a").composition
    tree_b = Harness(name="b").composition
    foreign = tree_a.child("research")
    paths_a = tree_a.paths()
    paths_b = tree_b.paths()

    with pytest.raises(ConfigurationError):
        tree_b.child("mine", parent=foreign)

    assert tree_a.paths() == paths_a
    assert tree_b.paths() == paths_b
    assert tree_a.get("/research/mine") is None
    assert tree_b.get("/mine") is None


def test_child_still_accepts_parents_from_its_own_tree() -> None:
    tree = Harness(name="a").composition
    parent = tree.child("research")

    defaulted = tree.child("scratch")
    scoped = tree.child("paper", parent=parent)
    by_path = tree.child("lab", parent="/research")
    created = tree.child("notes", parent="/deep/nested", create_parents=True)

    assert defaulted.path == "/scratch"
    assert scoped.path == "/research/paper"
    assert scoped.parent is parent
    assert scoped.tree is tree
    assert by_path.path == "/research/lab"
    assert created.path == "/deep/nested/notes"
