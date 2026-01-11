#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import inspect
import keyword
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


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


def generate_shim_file(
    *,
    import_name: str,
    exported_object_name: str,
    extras_name: Optional[str],
    summary: Dict[str, Any],
    include_private: bool,
    max_depth: int,
) -> str:
    extras_hint = (
        f"pip install dimos[{extras_name}]"
        if extras_name
        else "pip install dimos"
    )

    lines: List[str] = []
    lines.append("# Auto-generated optional-dependency shim")
    lines.append(f"# Target import: {import_name!r}")
    lines.append(f"# Exported object: {exported_object_name!r}")
    lines.append(f"# max_depth={max_depth}, include_private={include_private}")
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import importlib")
    lines.append("from typing import Any, Dict, Set, Optional")
    lines.append("")
    lines.append(f"__all__ = [{exported_object_name!r}]")
    lines.append("")
    lines.append("# ---- summary blob (the only per-module part) ----")
    lines.append(f"SUMMARY: Dict[str, Any] = {repr(summary)}")
    lines.append("")
    lines.append("")
    lines.append("class _MissingOptionalDependencyError(ModuleNotFoundError):")
    lines.append("    pass")
    lines.append("")
    lines.append("def _missing_dep_error(modname: str) -> _MissingOptionalDependencyError:")
    lines.append("    msg = (")
    lines.append('        f"Optional dependency {modname!r} is required for this feature. "')
    lines.append(f'        f"Install it with: {extras_hint!r}"')
    lines.append("    )")
    lines.append("    return _MissingOptionalDependencyError(msg)")
    lines.append("")
    lines.append("")
    lines.append("class _MissingTypeMeta(type):")
    lines.append("    def __call__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]")
    lines.append("        raise _missing_dep_error(getattr(cls, \"__shim_modname__\", cls.__name__))")
    lines.append("")
    lines.append("")
    lines.append("class _ShimRuntime:")
    lines.append("    \"\"\"")
    lines.append("    Shared runtime that interprets SUMMARY and produces shim objects.")
    lines.append("    Nodes are memoized by node-id so back-edges/cycles work.")
    lines.append("    \"\"\"")
    lines.append("")
    lines.append("    def __init__(self, modname: str, summary: Dict[str, Any]) -> None:")
    lines.append("        self._modname = modname")
    lines.append("        self._nodes: Dict[str, Any] = summary[\"nodes\"]")
    lines.append("        self._memo: Dict[str, Any] = {}")
    lines.append("")
    lines.append("    def get(self, node_id: int) -> Any:")
    lines.append("        sid = str(node_id)")
    lines.append("        if sid in self._memo:")
    lines.append("            return self._memo[sid]")
    lines.append("")
    lines.append("        n = self._nodes[sid]")
    lines.append("        kind = n[\"kind\"]")
    lines.append("")
    lines.append("        if kind == \"fn\":")
    lines.append("            def _missing_fn(*args: Any, **kwargs: Any) -> Any:")
    lines.append("                raise _missing_dep_error(self._modname)")
    lines.append("            self._memo[sid] = _missing_fn")
    lines.append("            return _missing_fn")
    lines.append("")
    lines.append("        if kind == \"type\":")
    lines.append("            # Unique type object for type hints; instantiation raises.")
    lines.append("            T = _MissingTypeMeta(f\"Missing_{self._modname}_{sid}\", (), {})")
    lines.append("            setattr(T, \"__shim_modname__\", self._modname)")
    lines.append("            self._memo[sid] = T")
    lines.append("            # Attach members via __getattr__-style namespace behavior on the *type*.")
    lines.append("            # We do this by setting a descriptor-like proxy object as an attribute,")
    lines.append("            # and also overriding __getattr__ on the type via a mixin method is messy.")
    lines.append("            # Instead: store a shim namespace on the type at _shim_ns and forward.")
    lines.append("            ns = _ShimNamespace(self, self._modname, sid)")
    lines.append("            setattr(T, \"_shim_ns\", ns)")
    lines.append("            def _type_getattr(self_or_cls: Any, name: str) -> Any:")
    lines.append("                return getattr(getattr(T, \"_shim_ns\"), name)")
    lines.append("            # Bind __getattr__ at the class level so MissingType.attr works.")
    lines.append("            setattr(T, \"__getattr__\", staticmethod(_type_getattr))")
    lines.append("            return T")
    lines.append("")
    lines.append("        if kind == \"ns\":")
    lines.append("            obj = _ShimNamespace(self, self._modname, sid)")
    lines.append("            self._memo[sid] = obj")
    lines.append("            return obj")
    lines.append("")
    lines.append("        raise RuntimeError(f\"Unknown node kind: {kind!r}\")")
    lines.append("")
    lines.append("")
    lines.append("class _ShimNamespace:")
    lines.append("    __slots__ = (\"_rt\", \"_modname\", \"_sid\")")
    lines.append("")
    lines.append("    def __init__(self, rt: _ShimRuntime, modname: str, sid: str) -> None:")
    lines.append("        self._rt = rt")
    lines.append("        self._modname = modname")
    lines.append("        self._sid = sid")
    lines.append("")
    lines.append("    def __getattr__(self, name: str) -> Any:")
    lines.append("        node = self._rt._nodes[self._sid]")
    lines.append("        if name in node.get(\"eager\", []):")
    lines.append("            raise _missing_dep_error(self._modname)")
    lines.append("        children = node.get(\"children\", {})")
    lines.append("        if name in children:")
    lines.append("            return self._rt.get(children[name])")
    lines.append("        raise AttributeError(name)")
    lines.append("")
    lines.append("    def __dir__(self) -> list[str]:")
    lines.append("        node = self._rt._nodes[self._sid]")
    lines.append("        return sorted(set(node.get(\"children\", {}).keys()) | set(node.get(\"eager\", [])))")
    lines.append("")
    lines.append("    def __repr__(self) -> str:")
    lines.append("        return f\"<MissingOptionalDependency shim {self._modname!r} node={self._sid}>\"")
    lines.append("")
    lines.append("")
    lines.append("class _LazyModuleProxy:")
    lines.append("    \"\"\"")
    lines.append("    Delay importing the real module until first attribute access.")
    lines.append("    Falls back to the shim runtime if the module is missing.")
    lines.append("    \"\"\"")
    lines.append("")
    lines.append("    __slots__ = (\"_modname\", \"_summary\", \"_loaded\", \"_obj\")")
    lines.append("")
    lines.append("    def __init__(self, modname: str, summary: Dict[str, Any]) -> None:")
    lines.append("        self._modname = modname")
    lines.append("        self._summary = summary")
    lines.append("        self._loaded = False")
    lines.append("        self._obj: Optional[Any] = None")
    lines.append("")
    lines.append("    def _load(self) -> Any:")
    lines.append("        if self._loaded:")
    lines.append("            return self._obj")
    lines.append("        try:")
    lines.append("            mod = importlib.import_module(self._modname)")
    lines.append("        except ModuleNotFoundError:")
    lines.append("            rt = _ShimRuntime(self._modname, self._summary)")
    lines.append("            mod = rt.get(self._summary[\"root\"])")
    lines.append("        self._obj = mod")
    lines.append("        self._loaded = True")
    lines.append("        return mod")
    lines.append("")
    lines.append("    def __getattr__(self, name: str) -> Any:")
    lines.append("        obj = self._load()")
    lines.append("        return getattr(obj, name)")
    lines.append("")
    lines.append("    def __dir__(self) -> list[str]:")
    lines.append("        if self._loaded and self._obj is not None:")
    lines.append("            return sorted(set(dir(self._obj)))")
    lines.append("        node = self._summary[\"nodes\"][str(self._summary[\"root\"])]")
    lines.append("        return sorted(set(node.get(\"children\", {}).keys()) | set(node.get(\"eager\", [])))")
    lines.append("")
    lines.append("    def __repr__(self) -> str:")
    lines.append("        if not self._loaded:")
    lines.append("            return f\"<LazyModuleProxy for {self._modname!r}>\"")
    lines.append("        return repr(self._obj)")
    lines.append("")
    lines.append("")
    lines.append(f"{exported_object_name} = _LazyModuleProxy({import_name!r}, SUMMARY)")
    lines.append("")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="shimgen2", description="Generate minimal JSON-summary shim module")
    ap.add_argument("module", help="Module import name to introspect (must be installed now), e.g. torch or cowsay")
    ap.add_argument("--extras", default=None, help="Extras group name (for error message), e.g. gpu -> dimos[gpu]")
    ap.add_argument("--output", default=None, help="Output filename (default: <top_level>.py)")
    ap.add_argument("--max-depth", type=int, default=4, help="Max recursion depth (default: 4)")
    ap.add_argument("--include-private", action="store_true", help="Include private members (names starting with _)")

    args = ap.parse_args(argv)
    import_name = args.module.strip()
    top_level = import_name.split(".", 1)[0]

    if not top_level.isidentifier():
        print(f"error: top-level name {top_level!r} is not a valid identifier", file=sys.stderr)
        return 2

    try:
        real_mod = importlib.import_module(import_name)
    except Exception as e:
        print(f"error: could not import {import_name!r} for introspection: {e!r}", file=sys.stderr)
        return 2

    summary = build_summary(real_mod, max_depth=args.max_depth, include_private=args.include_private)

    code = generate_shim_file(
        import_name=import_name,
        exported_object_name=top_level,
        extras_name=args.extras,
        summary=summary,
        include_private=args.include_private,
        max_depth=args.max_depth,
    )

    out_path = args.output or f"{top_level}.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)
        if not code.endswith("\n"):
            f.write("\n")

    print(f"Wrote shim to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
