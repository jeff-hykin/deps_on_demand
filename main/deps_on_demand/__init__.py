from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

DEFAULT_INSTALL_MESSAGE = None  # default; per-module install hints come from JSON


def _missing_dep_error(modname: str, install_message: Optional[str] = None) -> ModuleNotFoundError:
    msg = f"Optional dependency {modname!r} is required for this feature."
    hint = install_message or DEFAULT_INSTALL_MESSAGE
    if hint:
        msg += f"\n\n{hint}"
    return ModuleNotFoundError(msg)


class _MissingTypeMeta(type):
    def __call__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise _missing_dep_error(
            getattr(cls, "__shim_modname__", cls.__name__),
            getattr(cls, "__shim_install_message__", None),
        )


class _ShimRuntime:
    def __init__(self, modname: str, summary: Dict[str, Any], install_message: Optional[str]) -> None:
        self._modname = modname
        self._nodes: Dict[str, Any] = summary["nodes"]
        self._memo: Dict[str, Any] = {}
        self._install_message = install_message

    def get(self, node_id: int) -> Any:
        sid = str(node_id)
        if sid in self._memo:
            return self._memo[sid]

        n = self._nodes[sid]
        kind = n["kind"]

        if kind == "fn":
            def _missing_fn(*args: Any, **kwargs: Any) -> Any:
                raise _missing_dep_error(self._modname, self._install_message)
            self._memo[sid] = _missing_fn
            return _missing_fn

        if kind == "type":
            T = _MissingTypeMeta(f"Missing_{self._modname}_{sid}", (), {})
            setattr(T, "__shim_modname__", self._modname)
            setattr(T, "__shim_install_message__", self._install_message)
            self._memo[sid] = T
            ns = _ShimNamespace(self, self._modname, sid)
            setattr(T, "_shim_ns", ns)
            def _type_getattr(self_or_cls: Any, name: str) -> Any:
                return getattr(getattr(T, "_shim_ns"), name)
            setattr(T, "__getattr__", staticmethod(_type_getattr))
            return T

        if kind == "ns":
            obj = _ShimNamespace(self, self._modname, sid)
            self._memo[sid] = obj
            return obj

        raise RuntimeError(f"Unknown node kind: {kind!r}")


class _ShimNamespace:
    __slots__ = ("_rt", "_modname", "_sid")

    def __init__(self, rt: _ShimRuntime, modname: str, sid: str) -> None:
        self._rt = rt
        self._modname = modname
        self._sid = sid

    def __getattr__(self, name: str) -> Any:
        node = self._rt._nodes[self._sid]
        if name in node.get("eager", []):
            raise _missing_dep_error(self._modname, self._rt._install_message)
        children = node.get("children", {})
        if name in children:
            return self._rt.get(children[name])
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        node = self._rt._nodes[self._sid]
        return sorted(set(node.get("children", {}).keys()) | set(node.get("eager", [])))

    def __repr__(self) -> str:
        return f"<MissingOptionalDependency shim {self._modname!r} node={self._sid}>"


class LazyModuleProxy:
    """
    Lazily import the real module on first attribute access, or fall back to a
    shim built from a stored JSON summary. Accepts either:
      - (modname, summary_dict) for direct construction, or
      - (stem_name, base_path) where base_path/stem_name.json holds {"module", "summary"}.
    """

    __slots__ = (
        "_stem",
        "_base",
        "_modname",
        "_summary",
        "_install_message",
        "_explicit_children",
        "_explicit_trie",
        "_loaded",
        "_obj",
    )

    def __init__(self, mod_identifier: str, summary_or_base: Any) -> None:
        self._stem = mod_identifier
        self._base: Optional[Path] = None
        self._modname: Optional[str] = None
        self._summary: Optional[Dict[str, Any]] = None
        self._install_message: Optional[str] = None
        self._explicit_children: Set[str] = set()
        self._explicit_trie: Dict[str, Any] = {}
        self._loaded = False
        self._obj: Optional[Any] = None

        if isinstance(summary_or_base, dict):
            self._modname = mod_identifier
            self._summary = summary_or_base
        else:
            self._base = Path(summary_or_base)

    def _ensure_summary(self) -> None:
        if self._summary is not None and self._modname is not None:
            return
        if self._base is None:
            raise RuntimeError("LazyModuleProxy missing summary and base path")
        path = self._base / f"{self._stem}.json"
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"shim summary file not found: {path}") from e
        self._modname = data.get("module", self._stem)
        self._summary = data.get("summary")
        extra = data.get("extra")
        if extra:
            self._install_message = f"pip install .[{extra}]"
        self._explicit_children = set(data.get("explicit_child_modules", []))
        self._explicit_trie = self._build_trie(self._explicit_children)
        if self._summary is None:
            raise RuntimeError(f"shim summary missing for {self._stem!r} in {path}")

    def _build_trie(self, paths: Set[str]) -> Dict[str, Any]:
        trie: Dict[str, Any] = {}
        if not self._modname:
            return trie
        prefix = f"{self._modname}."
        for p in paths:
            if not p.startswith(prefix):
                continue
            segs = p[len(prefix) :].split(".")
            node = trie
            for i, seg in enumerate(segs):
                node = node.setdefault(seg, {"module": None, "children": {}})
                if i == len(segs) - 1:
                    node["module"] = p
                else:
                    node = node["children"]
        return trie

    def _resolve_loaded_attr(self, segments: list[str]) -> Any:
        obj = self._load()
        for seg in segments:
            obj = getattr(obj, seg)
        return obj

    def _load(self, module_to_import: Optional[str] = None) -> Any:
        if self._loaded:
            return self._obj

        self._ensure_summary()
        assert self._modname is not None
        assert self._summary is not None

        target = module_to_import or self._modname
        try:
            importlib.import_module(target)
            # Ensure root module object is returned.
            mod = importlib.import_module(self._modname)
        except ModuleNotFoundError:
            rt = _ShimRuntime(self._modname, self._summary, self._install_message)
            mod = rt.get(self._summary["root"])
        self._obj = mod
        self._loaded = True
        return mod

    def __getattr__(self, name: str) -> Any:
        if not self._loaded:
            self._ensure_summary()
        # If the attribute is an explicit child subtree, return an intermediate proxy
        # that will import the child module when deeper attributes are accessed.
        if not self._loaded and name in self._explicit_trie:
            return _IntermediateNamespace(self, self._explicit_trie[name], [name])

        obj = self._load()
        try:
            return getattr(obj, name)
        except AttributeError:
            # If the requested attribute belongs to an explicit child module, make sure
            # that child is imported before retrying (handles packages that don't
            # auto-load their submodules).
            for child in self._explicit_children:
                tail = child.rsplit(".", 1)[-1]
                if child.startswith(f"{self._modname}.") and tail == name:
                    try:
                        importlib.import_module(child)
                    except BaseException:
                        pass
                    break
        return getattr(obj, name)

    def __dir__(self) -> list[str]:
        if self._loaded and self._obj is not None:
            return sorted(set(dir(self._obj)))
        self._ensure_summary()
        assert self._summary is not None
        node = self._summary["nodes"][str(self._summary["root"])]
        names = set(node.get("children", {}).keys()) | set(node.get("eager", []))
        names |= set(self._explicit_trie.keys())
        return sorted(names)

    def __repr__(self) -> str:
        if not self._loaded:
            return f"<LazyModuleProxy for {self._stem!r}>"
        return repr(self._obj)


class _IntermediateNamespace:
    __slots__ = ("_proxy", "_node", "_segments")

    def __init__(self, proxy: LazyModuleProxy, trie_node: Dict[str, Any], segments: list[str]) -> None:
        self._proxy = proxy
        self._node = trie_node
        self._segments = segments

    def __getattr__(self, name: str) -> Any:
        # If root already loaded, resolve directly.
        if self._proxy._loaded:
            return self._proxy._resolve_loaded_attr(self._segments + [name])

        children = self._node.get("children", {})
        if name in children:
            child_node = children[name]
            if child_node.get("module"):
                # Leaf: import the child module, then resolve attribute chain.
                self._proxy._load(module_to_import=child_node["module"])
                return self._proxy._resolve_loaded_attr(self._segments + [name])
            return _IntermediateNamespace(self._proxy, child_node, self._segments + [name])

        # If this node itself represents a module, try importing it before resolving.
        module_path = self._node.get("module")
        if module_path:
            self._proxy._load(module_to_import=module_path)
            return self._proxy._resolve_loaded_attr(self._segments + [name])

        # Fallback: load root and resolve.
        return self._proxy._resolve_loaded_attr(self._segments + [name])

    def __dir__(self) -> list[str]:
        names = set(self._node.get("children", {}).keys())
        if self._node.get("module"):
            try:
                obj = self._proxy._resolve_loaded_attr(self._segments)
                names |= set(dir(obj))
            except Exception:
                pass
        return sorted(names)

    def __repr__(self) -> str:
        return f"<IntermediateNamespace {'.'.join(self._segments)}>"
