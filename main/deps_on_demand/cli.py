#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import keyword
import pkgutil
import re
import sys
import warnings
from contextlib import redirect_stdout, redirect_stderr, contextmanager
from io import StringIO

import importlib.metadata as importlib_metadata
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


def _normalize_name(name: str) -> str:
    """PEP 503-style normalization: lowercase and replace runs of -_. with -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _submodules_requiring_import(modname: str) -> List[str]:
    """
    List submodules under `modname` that are not already accessible as attributes
    after importing `modname` itself.
    """
    root = _quiet_import(modname)
    if not hasattr(root, "__path__"):
        return []

    base_parts = modname.split(".")
    need_import: List[str] = []

    with _silence_imports():
        for info in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
            full = info.name
            rel_parts = full.split(".")[len(base_parts):]

            obj = root
            missing = False
            for part in rel_parts:
                if not hasattr(obj, part):
                    missing = True
                    break
                obj = getattr(obj, part)
            if missing:
                need_import.append(full)

    return sorted(need_import)


def _quiet_import(modname: str) -> Any:
    buf = StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with redirect_stdout(buf), redirect_stderr(buf):
            return importlib.import_module(modname)


@contextmanager
def _silence_imports() -> Any:
    buf = StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with redirect_stdout(buf), redirect_stderr(buf):
            yield


def build_summary(
    root_obj: Any,
    *,
    include_private: bool,
) -> Dict[str, Any]:
    """
    Build a cyclic-safe summary graph without recursive traversal (avoids stack overflow).

    Back-edges are represented by reusing node IDs (object identity).
    """
    objid_to_nodeid: Dict[int, int] = {}
    nodes: Dict[int, SumNode] = {}
    expanded: Set[int] = set()
    nid_to_obj: Dict[int, Any] = {}
    next_id = 0
    to_expand: List[Tuple[Any, int]] = []

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

    def schedule(obj: Any) -> int:
        kind = classify_value(obj)
        if kind == "eager":
            raise AssertionError("schedule() should not be called for eager values")
        nid = get_node_id(obj, kind)
        if nid not in expanded and nid not in nid_to_obj:
            nid_to_obj[nid] = obj
            to_expand.append((obj, nid))
        return nid

    root_id = schedule(root_obj)

    while to_expand:
        obj, nid = to_expand.pop()
        if nid in expanded:
            continue
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

            child_id = schedule(val)
            node.children[name] = child_id

        expanded.add(nid)

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

def _build_pip_to_modules_map() -> Dict[str, List[str]]:
    pip_to_mods: Dict[str, List[str]] = {}
    for mod, dists in importlib_metadata.packages_distributions().items():
        for dist in dists:
            key = _normalize_name(dist)
            pip_to_mods.setdefault(key, []).append(mod)
    return pip_to_mods


def _parse_extra_modules(
    pyproject_path: Path,
    extra_name: str,
    pip_to_modules: Dict[str, List[str]],
) -> List[Tuple[str, str, str]]:
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    extras = (
        data.get("project", {})
        .get("optional-dependencies", {})
    )
    if extra_name not in extras:
        raise KeyError(f"extra {extra_name!r} not found in optional-dependencies")

    deps: List[str] = extras[extra_name]
    modules: List[Tuple[str, str, str]] = []
    seen: Set[str] = set()
    for dep in deps:
        m = re.match(r"[A-Za-z0-9_.-]+", dep)
        if not m:
            continue
        pip_name = m.group(0).split("[", 1)[0]
        pip_key = _normalize_name(pip_name)
        mod_candidates = pip_to_modules.get(pip_key)
        if not mod_candidates:
            raise KeyError(f"could not resolve import module for pip package {pip_name!r}")
        for import_name in mod_candidates:
            symbol_name = sanitize_identifier(import_name.split(".", 1)[0])
            if symbol_name in seen:
                continue
            seen.add(symbol_name)
            modules.append((pip_name, import_name, symbol_name))
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
    # Silence noisy deprecations during introspection/import.
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

    ap = argparse.ArgumentParser(prog="shimgen2", description="Generate optional-dependency shim JSON bundle for an extra")
    ap.add_argument("extra", help="Extra name from [project.optional-dependencies]")
    ap.add_argument("pyproject", nargs="?", default="pyproject.toml", help="Path to pyproject.toml (default: pyproject.toml)")
    ap.add_argument("--include-private", action="store_true", help="Include private members (names starting with _)")

    args = ap.parse_args(argv)
    pyproject_path = Path(args.pyproject)
    if not pyproject_path.exists():
        print(f"error: pyproject file not found: {pyproject_path}", file=sys.stderr)
        return 2

    pip_to_modules = _build_pip_to_modules_map()

    try:
        modules = _parse_extra_modules(pyproject_path, args.extra, pip_to_modules)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    imports_dir = Path("imports")
    imports_dir.mkdir(parents=True, exist_ok=True)

    written: List[Tuple[str, str, str]] = []
    for pip_name, import_name, symbol_name in modules:
        try:
            real_mod = _quiet_import(import_name)
        except Exception as e:
            print(f"error: could not import {import_name!r} for introspection: {e!r}", file=sys.stderr)
            return 2

        explicit_children = _submodules_requiring_import(import_name)
        for child in explicit_children:
            # Skip private/internal submodules.
            parts = child.split(".")
            if any(part.startswith("_") for part in parts):
                continue
            if "tests" in parts or "testing" in parts:
                continue
            try:
                _quiet_import(child)
            except BaseException as e:
                print(f"warning: skipped submodule {child!r} due to import error: {e!r}", file=sys.stderr)

        with _silence_imports():
            summary = build_summary(real_mod, include_private=args.include_private)
        out_path = imports_dir / f"{symbol_name}.json"
        if out_path.exists():
            out_path.unlink()
        payload = {
            "pip_name": pip_name,
            "module": import_name,
            "summary": summary,
            "extra": args.extra,
            "explicit_child_modules": explicit_children,
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        written.append((pip_name, import_name, symbol_name))

    _write_imports_init(imports_dir)

    print(f"Wrote shims for extra {args.extra!r}: {', '.join(symbol for _, _, symbol in written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
