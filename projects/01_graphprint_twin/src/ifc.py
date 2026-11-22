"""IFC GlobalId helpers, so graph nodes can be linked to BIM models.

IFC (ISO 16739) identifies every object by a 22-character ``GlobalId``: a
128-bit GUID packed into a custom base-64 alphabet. Storing that id on a
``WallSegment`` lets a BIM tool (Revit, Archicad, BlenderBIM, IfcOpenShell) join
print telemetry and QC results to the wall element in the building model.

The compression follows the reference algorithm used by IfcOpenShell
(``ifcopenshell.guid.compress``).
"""

from __future__ import annotations

import uuid

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"

# Fixed namespace so the same element name always maps to the same GlobalId.
BIOPRINT_NAMESPACE = uuid.UUID("6f1d3c52-8a0e-4c7b-9a51-2b7e0f4d9c11")


def compress(guid: uuid.UUID) -> str:
    """Pack a UUID into a 22-character IFC GlobalId."""
    b = guid.bytes

    def encode(value: int, chars: int) -> str:
        out = []
        for _ in range(chars):
            out.append(_ALPHABET[value % 64])
            value //= 64
        return "".join(reversed(out))

    parts = [encode(b[0], 2)]
    for i in range(1, 16, 3):
        parts.append(encode((b[i] << 16) | (b[i + 1] << 8) | b[i + 2], 4))
    return "".join(parts)


def expand(global_id: str) -> uuid.UUID:
    """Inverse of :func:`compress`."""
    if len(global_id) != 22 or any(c not in _ALPHABET for c in global_id):
        raise ValueError(f"not an IFC GlobalId: {global_id!r}")

    def decode(chunk: str) -> int:
        value = 0
        for c in chunk:
            value = value * 64 + _ALPHABET.index(c)
        return value

    raw = bytearray([decode(global_id[:2])])
    for i in range(2, 22, 4):
        v = decode(global_id[i : i + 4])
        raw += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return uuid.UUID(bytes=bytes(raw))


def global_id_for(name: str) -> str:
    """Deterministic IFC GlobalId for a named element (uuid5 in the BioPrint namespace)."""
    return compress(uuid.uuid5(BIOPRINT_NAMESPACE, name))
