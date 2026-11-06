"""Minimal interpreter for the IEC 61131-3 Structured Text subset used in plc/src.

This is NOT a PLC compiler or runtime. It exists for one job: letting CI check
that the *logic* of FB_BioPrintPID.st and FB_WestgardMonitor.st matches the
Python reference, by replaying the reference vectors through the actual ST
source. A real compile in TwinCAT or CODESYS is still needed to confirm syntax,
types and timing on a target.

Supported subset: one FUNCTION_BLOCK per file; VAR_INPUT / VAR_OUTPUT / VAR /
VAR CONSTANT declarations (scalars and one-dimensional ARRAY[1..n]); ``:=``
assignments; IF / ELSIF / ELSE / END_IF; FOR ... TO ... DO / END_FOR; RETURN;
AND / OR / NOT; comparisons (``= <> < > <= >=``); and the functions ABS, MAX,
MIN, LIMIT, SQRT, INT_TO_LREAL and INT_TO_DINT. Anything else raises an error
instead of being guessed.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

_COMMENT = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_DECL = re.compile(
    r"^\s*(\w+)\s*:\s*(ARRAY\s*\[\s*\d+\s*\.\.\s*\d+\s*\]\s*OF\s+\w+|\w+)\s*(?::=\s*([^;]+))?;"
)
_FUNCS = {
    "ABS": "abs",
    "MAX": "max",
    "MIN": "min",
    "LIMIT": "_limit",
    "SQRT": "_sqrt",
    "INT_TO_LREAL": "float",
    "INT_TO_DINT": "int",
}


def _limit(lo: float, x: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _sqrt(x: float) -> float:
    return math.sqrt(x)


def _default(type_name: str, init: str | None) -> Any:
    if type_name.upper().startswith("ARRAY"):
        return defaultdict(float)
    if init is not None:
        init = init.strip()
        if init.upper() in {"TRUE", "FALSE"}:
            return init.upper() == "TRUE"
        return float(init) if type_name.upper() in {"REAL", "LREAL"} else int(float(init))
    return {"BOOL": False, "REAL": 0.0, "LREAL": 0.0}.get(type_name.upper(), 0)


class STFunctionBlock:
    """Load an ST function block and call it like a PLC would: ``fb(input=...)``."""

    def __init__(self, path: str | Path) -> None:
        text = _COMMENT.sub(" ", Path(path).read_text())
        match = re.search(r"FUNCTION_BLOCK\s+(\w+)(.*?)END_FUNCTION_BLOCK", text, re.DOTALL)
        if not match:
            raise ValueError(f"{path}: no FUNCTION_BLOCK found")
        self.name = match.group(1)
        inner = match.group(2)

        self.vars: dict[str, Any] = {}
        self.inputs: set[str] = set()
        blocks = list(
            re.finditer(r"(VAR_INPUT|VAR_OUTPUT|VAR CONSTANT|VAR)\b(.*?)END_VAR", inner, re.DOTALL)
        )
        for block in blocks:
            for line in block.group(2).splitlines():
                decl = _DECL.match(line)
                if decl:
                    name, type_name, init = decl.groups()
                    self.vars[name] = _default(type_name, init)
                    if block.group(1) == "VAR_INPUT":
                        self.inputs.add(name)
        body = inner[blocks[-1].end() :]
        self._code = compile(self._translate(body), f"<{self.name}>", "exec")

    # -- translation -------------------------------------------------------------

    def _expr(self, expr: str) -> str:
        expr = re.sub(r"<>", " != ", expr)
        expr = re.sub(r"(?<![<>:!=])=(?!=)", " == ", expr)

        def ident(m: re.Match[str]) -> str:
            word = m.group(0)
            upper = word.upper()
            if upper in _FUNCS:
                return _FUNCS[upper]
            if upper in {"AND", "OR", "NOT"}:
                return f" {upper.lower()} "
            if upper in {"TRUE", "FALSE"}:
                return "True" if upper == "TRUE" else "False"
            if word in self.vars:
                return f"v[{word!r}]"
            raise ValueError(f"{self.name}: unsupported identifier '{word}' in: {expr.strip()}")

        # Identifiers, but not exponents inside numeric literals such as 1.0E-6.
        return re.sub(r"(?<![\d.])\b[A-Za-z_]\w*\b", ident, expr)

    def _translate(self, body: str) -> str:
        src = " ".join(body.split())
        token = re.compile(
            r"\s*(?:"
            r"(?P<elsif>ELSIF\s+(?P<ec>.+?)\s+THEN)"
            r"|(?P<if>IF\s+(?P<ic>.+?)\s+THEN)"
            r"|(?P<else>ELSE)\b"
            r"|(?P<endif>END_IF\s*;?)"
            r"|(?P<for>FOR\s+(?P<fv>\w+)\s*:=\s*(?P<fa>.+?)\s+TO\s+(?P<fb>.+?)\s+DO)"
            r"|(?P<endfor>END_FOR\s*;?)"
            r"|(?P<ret>RETURN\s*;)"
            r"|(?P<assign>(?P<lhs>\w+(?:\[[^\]]+\])?)\s*:=\s*(?P<rhs>[^;]+);)"
            r")"
        )
        lines: list[str] = []
        depth, pos = 1, 0
        pad = "    "
        while pos < len(src):
            m = token.match(src, pos)
            if not m or m.end() == pos:
                if src[pos:].strip() == "":
                    break
                raise ValueError(f"{self.name}: cannot parse near: {src[pos : pos + 60]!r}")
            pos = m.end()
            if m.group("elsif"):
                lines.append(pad * (depth - 1) + f"elif {self._expr(m.group('ec'))}:")
            elif m.group("if"):
                lines.append(pad * depth + f"if {self._expr(m.group('ic'))}:")
                depth += 1
            elif m.group("else"):
                lines.append(pad * (depth - 1) + "else:")
            elif m.group("endif") or m.group("endfor"):
                lines.append(pad * depth + "pass")
                depth -= 1
            elif m.group("for"):
                var = m.group("fv")
                lines.append(
                    pad * depth + f"for _i in range(int({self._expr(m.group('fa'))}), "
                    f"int({self._expr(m.group('fb'))}) + 1):"
                )
                depth += 1
                lines.append(pad * depth + f"v[{var!r}] = _i")
            elif m.group("ret"):
                lines.append(pad * depth + "return")
            else:
                lhs = m.group("lhs")
                arr = re.match(r"(\w+)\[(.+)\]", lhs)
                target = (
                    f"v[{arr.group(1)!r}][int({self._expr(arr.group(2))})]"
                    if arr
                    else f"v[{lhs!r}]"
                )
                lines.append(pad * depth + f"{target} = {self._expr(m.group('rhs'))}")
        if depth != 1:
            raise ValueError(f"{self.name}: unbalanced IF/FOR blocks")
        return "def _scan(v):\n" + "\n".join(lines) + "\n    pass\n"

    # -- execution -----------------------------------------------------------------

    def __call__(self, **inputs: Any) -> STFunctionBlock:
        for key, value in inputs.items():
            if key not in self.inputs:
                raise KeyError(f"{self.name} has no input '{key}'")
            self.vars[key] = value
        scope: dict[str, Any] = {"_limit": _limit, "_sqrt": _sqrt}
        exec(self._code, scope)  # noqa: S102 - trusted, repository-local source
        scope["_scan"](self.vars)
        return self

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["vars"][name]
        except KeyError:
            raise AttributeError(name) from None
