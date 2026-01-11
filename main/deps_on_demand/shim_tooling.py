from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ['torch']

INSTALL_MESSAGE = 'pip install .[misc]'

def _missing_dep_error(modname: str) -> ModuleNotFoundError:
    msg = (
        f"Optional dependency {modname!r} is required for this feature. "
        f"Install it with: {INSTALL_MESSAGE}"
    )
    return ModuleNotFoundError(msg)


class _MissingTypeMeta(type):
    def __call__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise _missing_dep_error(getattr(cls, "__shim_modname__", cls.__name__))


class _ShimRuntime:
    def __init__(self, modname: str, summary: Dict[str, Any]) -> None:
        self._modname = modname
        self._nodes: Dict[str, Any] = summary["nodes"]
        self._memo: Dict[str, Any] = {}

    def get(self, node_id: int) -> Any:
        sid = str(node_id)
        if sid in self._memo:
            return self._memo[sid]

        n = self._nodes[sid]
        kind = n["kind"]

        if kind == "fn":
            def _missing_fn(*args: Any, **kwargs: Any) -> Any:
                raise _missing_dep_error(self._modname)
            self._memo[sid] = _missing_fn
            return _missing_fn

        if kind == "type":
            T = _MissingTypeMeta(f"Missing_{self._modname}_{sid}", (), {})
            setattr(T, "__shim_modname__", self._modname)
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
            raise _missing_dep_error(self._modname)
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
    __slots__ = ("_modname", "_summary", "_loaded", "_obj")

    def __init__(self, modname: str, summary: Dict[str, Any]) -> None:
        self._modname = modname
        self._summary = summary
        self._loaded = False
        self._obj: Optional[Any] = None

    def _load(self) -> Any:
        if self._loaded:
            return self._obj
        try:
            mod = importlib.import_module(self._modname)
        except ModuleNotFoundError:
            rt = _ShimRuntime(self._modname, self._summary)
            mod = rt.get(self._summary["root"] )
        self._obj = mod
        self._loaded = True
        return mod

    def __getattr__(self, name: str) -> Any:
        obj = self._load()
        return getattr(obj, name)

    def __dir__(self) -> list[str]:
        if self._loaded and self._obj is not None:
            return sorted(set(dir(self._obj)))
        node = self._summary["nodes"][str(self._summary["root"])]
        return sorted(set(node.get("children", {}).keys()) | set(node.get("eager", [])))

    def __repr__(self) -> str:
        if not self._loaded:
            return f"<LazyModuleProxy for {self._modname!r}>"
        return repr(self._obj)

