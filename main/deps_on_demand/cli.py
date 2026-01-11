#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import keyword
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import tomllib


# -------------------------
# Summary data model
# -------------------------

@dataclass
class SumNode:
    kind: str                    # "ns" | "type" | "fn"
    children: Dict[str, int] = field(default_factory=dict)
    eager: Set[str] = field(default_factory=set)


def is_public_name(name: str, include_private: bool) -> bool:
    return include_private or not name.startswith("_")


def safe_iter_members(obj: Any) -> List[Tuple[str, Any]]:
    """
    Prefer __dict__ to avoid triggering properties/descriptors.
    """
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return list(d.items())
    return []


def classify_value(v: Any) -> str:
    """
    Return one of: "ns" | "type" | "fn" | "eager"
    """
    if inspect.ismodule(v):
        return "ns"

    if inspect.isclass(v):
        # builtins like int/list/str should be eager (raise on-get)
        if getattr(v, "__module__", None) == "builtins":
            return "eager"
        return "type"

    if inspect.isfunction(v) or inspect.isbuiltin(v) or inspect.ismethoddescriptor(v):
        return "fn"

    # Other callables (instances w/ __call__) -> treat as eager on-get
    if callable(v):
        return "eager"

    return "eager"


def sanitize_identifier(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if not s:
        s = "_"
    if s[0].isdigit():
        s = "_" + s
    if keyword.iskeyword(s):
        s += "_"
    return s


def build_summary(
    root_obj: Any,
    *,
    max_depth: int,
    include_private: bool,
) -> Dict[str, Any]:
    """
    Build a cyclic-safe summary graph.

    Back-edges are represented by reusing node IDs (object identity).
    """
    objid_to_nodeid: Dict[int, int] = {}
    nodes: Dict[int, SumNode] = {}
    next_id = 0

    def get_node_id(obj: Any, kind: str) -> int:
        nonlocal next_id
        oid = id(obj)
        if oid in objid_to_nodeid:
            return objid_to_nodeid[oid]
        nid = next_id
        next_id += 1
        objid_to_nodeid[oid] = nid
        nodes[nid] = SumNode(kind=kind)
        return nid

    def walk(obj: Any, depth: int) -> int:
        kind = classify_value(obj)
        if kind == "eager":
            raise AssertionError("walk() should not be called for eager values")

        nid = get_node_id(obj, kind)

        # If already expanded, avoid re-walking to prevent cycles
        # (still OK: children may already be partially filled)
        if depth >= max_depth:
            return nid

        # Expand
        node = nodes[nid]
        for name, val in safe_iter_members(obj):
            if not isinstance(name, str):
                continue
            if not is_public_name(name, include_private):
                continue

            try:
                ck = classify_value(val)
            except Exception:
                node.eager.add(name)
                continue

            if ck == "eager":
                node.eager.add(name)
                continue

            # recurse
            child_id = walk(val, depth + 1)
            node.children[name] = child_id

        return nid

    root_id = walk(root_obj, 0)

    # Convert nodes to JSON-serializable form
    out_nodes: Dict[str, Any] = {}
    for nid, n in nodes.items():
        out_nodes[str(nid)] = {
            "kind": n.kind,
            "children": dict(sorted(n.children.items())),
            "eager": sorted(n.eager),
        }

    return {
        "root": root_id,
        "nodes": out_nodes,
    }

def _parse_extra_modules(pyproject_path: Path, extra_name: str) -> List[Tuple[str, str]]:
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    extras = (
        data.get("project", {})
        .get("optional-dependencies", {})
    )
    if extra_name not in extras:
        raise KeyError(f"extra {extra_name!r} not found in optional-dependencies")

    deps: List[str] = extras[extra_name]
    modules: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for dep in deps:
        # Extract the base package token and turn it into an importable name.
        m = re.match(r"[A-Za-z0-9_.-]+", dep)
        if not m:
            continue
        pkg = m.group(0)
        pkg = pkg.split("[", 1)[0]
        import_name = pkg.replace("-", "_")
        symbol_name = sanitize_identifier(import_name.split(".", 1)[0])
        if symbol_name in seen:
            continue
        seen.add(symbol_name)
        modules.append((import_name, symbol_name))
    return modules


def _write_imports_init(
    imports_dir: Path,
) -> None:
    lines: List[str] = []
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import json")
    lines.append("from pathlib import Path")
    lines.append("import deps_on_demand")
    lines.append("")
    lines.append("names: list[str] = []")
    lines.append("base = Path(__file__).parent")
    lines.append("for path in base.glob(\"*.json\"):")
    lines.append("    name = path.stem")
    lines.append("    names.append(name)")
    lines.append("    with path.open(\"r\", encoding=\"utf-8\") as f:")
    lines.append("        data = json.load(f)")
    lines.append("    globals()[name] = deps_on_demand.LazyModuleProxy(name, base)")
    lines.append("")
    lines.append("__all__ = names")
    lines.append("")

    imports_dir.joinpath("__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="shimgen2", description="Generate optional-dependency shim JSON bundle for an extra")
    ap.add_argument("extra", help="Extra name from [project.optional-dependencies]")
    ap.add_argument("pyproject", nargs="?", default="pyproject.toml", help="Path to pyproject.toml (default: pyproject.toml)")
    ap.add_argument("--max-depth", type=int, default=4, help="Max recursion depth (default: 4)")
    ap.add_argument("--include-private", action="store_true", help="Include private members (names starting with _)")

    args = ap.parse_args(argv)
    pyproject_path = Path(args.pyproject)
    if not pyproject_path.exists():
        print(f"error: pyproject file not found: {pyproject_path}", file=sys.stderr)
        return 2

    try:
        modules = _parse_extra_modules(pyproject_path, args.extra)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    imports_dir = Path("imports")
    imports_dir.mkdir(parents=True, exist_ok=True)

    written: List[Tuple[str, str]] = []
    for import_name, symbol_name in modules:
        try:
            real_mod = importlib.import_module(import_name)
        except Exception as e:
            print(f"error: could not import {import_name!r} for introspection: {e!r}", file=sys.stderr)
            return 2

        summary = build_summary(real_mod, max_depth=args.max_depth, include_private=args.include_private)
        out_path = imports_dir / f"{symbol_name}.json"
        payload = {"module": import_name, "summary": summary, "extra": args.extra}
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        written.append((import_name, symbol_name))

    _write_imports_init(imports_dir)

    print(f"Wrote shims for extra {args.extra!r}: {', '.join(symbol for _, symbol in written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
