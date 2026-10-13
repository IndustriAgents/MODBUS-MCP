"""Every JSON tool example in the READMEs works against the mock device.

The examples used to point at addresses the mock does not have (100, 200), so
they failed with Illegal Data Address before showing anything. Each README's
own tag-map example is loaded as REGISTER_MAP_FILE for its tag examples.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
from test_stdio_integration import mock, server  # noqa: F401 - module-scoped mock fixture

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROOT = Path(__file__).resolve().parents[2]
READMES = [ROOT / "README.md", ROOT / "modbus-python" / "README.md"]
BLOCK = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def examples(readme: Path) -> tuple[dict, list[dict]]:
    tag_maps, calls = [], []
    for block in BLOCK.findall(readme.read_text()):
        data = json.loads(textwrap.dedent(block))
        if "tool" in data:
            calls.append(data)
        elif data and all(isinstance(v, dict) and "address" in v for v in data.values()):
            tag_maps.append(data)
    assert len(tag_maps) == 1, f"{readme.name}: expected one tag-map example"
    return tag_maps[0], calls


@pytest.mark.parametrize("readme", READMES, ids=lambda p: str(p.relative_to(ROOT)))
async def test_readme_examples_run_against_the_mock(readme: Path, mock, tmp_path) -> None:  # noqa: F811
    tag_map, calls = examples(readme)
    assert len(calls) >= 10
    tags = tmp_path / "tags.json"
    tags.write_text(json.dumps(tag_map))
    async with server(
        mock, tmp_path, MODBUS_WRITES_ENABLED="true", REGISTER_MAP_FILE=str(tags)
    ) as srv:
        for call in calls:
            envelope = await srv.call(call["tool"], **call["parameters"])
            assert envelope["success"], (call, envelope)
