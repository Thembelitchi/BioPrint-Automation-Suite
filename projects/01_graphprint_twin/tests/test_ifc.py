"""IFC GlobalId compression (matches IfcOpenShell's reference algorithm)."""

import uuid

import pytest
from ifc import compress, expand, global_id_for


def test_known_vectors() -> None:
    assert compress(uuid.UUID(int=0)) == "0" * 22
    assert compress(uuid.UUID(int=(1 << 128) - 1)) == "3" + "$" * 21


def test_round_trip() -> None:
    for _ in range(200):
        g = uuid.uuid4()
        gid = compress(g)
        assert len(gid) == 22 and gid[0] in "0123"
        assert expand(gid) == g


def test_deterministic_per_name() -> None:
    assert global_id_for("Studio_Unit_BackWall") == global_id_for("Studio_Unit_BackWall")
    assert global_id_for("Studio_Unit_BackWall") != global_id_for("Studio_Unit_FrontWall")


@pytest.mark.parametrize("bad", ["short", "0" * 21 + "!", "0" * 23])
def test_expand_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        expand(bad)
