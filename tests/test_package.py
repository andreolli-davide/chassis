from __future__ import annotations

import chassis


def test_package_exposes_version() -> None:
    assert isinstance(chassis.__version__, str)
    assert chassis.__version__


def test_package_identity_is_chassis() -> None:
    assert chassis.__name__ == "chassis"
