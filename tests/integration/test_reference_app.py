"""The production reference application runs and passes its own assertions
(roadmap R034).

The application is documentation that executes: this test runs it exactly as a
user would and asserts the documented transcript, so the example cannot rot.
"""

from __future__ import annotations

import asyncio

import pytest
from examples.production_reference.app import main

EXPECTED_LINES = [
    "[1] preview shows two additions and zero mutation",
    "[2] configuration applied; analytics scope narrows tools to lookup_order",
    "[3] ambiguity reported with candidates, resolved by preference",
    "[4] support-agent@1 materialized with its scoped composition",
    "[5] invoke answered on generation gen_0003 (revision 1)",
    "[6] stream produced 2 attributed events",
    "[7] policy denied the ungranted permission",
    "[8] secret absent from replay records, snapshot, and diagnostics",
    "[9] recording replayed 15 records without live calls",
    "[10] provider replaced while the old run stayed pinned to its generation",
    "[11] snapshot 08eba156a7d3 attributed; 1 live generation(s), 4 instance(s)",
    "[12] failed setup rolled back every effect; the previous generation stayed current",
    "[13] graceful shutdown; 4 instance(s) returned to baseline",
    "support desk: every scenario held",
]


def test_the_reference_application_passes_its_own_assertions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(main())

    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines == EXPECTED_LINES


def test_the_reference_application_is_deterministic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(main())
    first = capsys.readouterr().out
    asyncio.run(main())
    second = capsys.readouterr().out

    assert first == second
