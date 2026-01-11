from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

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

    __slots__ = ("_stem", "_base", "_modname", "_summary", "_install_message", "_loaded", "_obj")

    def __init__(self, mod_identifier: str, summary_or_base: Any) -> None:
        self._stem = mod_identifier
        self._base: Optional[Path] = None
        self._modname: Optional[str] = None
        self._summary: Optional[Dict[str, Any]] = None
        self._install_message: Optional[str] = None
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
        if self._summary is None:
            raise RuntimeError(f"shim summary missing for {self._stem!r} in {path}")

    def _load(self) -> Any:
        if self._loaded:
            return self._obj

        self._ensure_summary()
        assert self._modname is not None
        assert self._summary is not None

        try:
            mod = importlib.import_module(self._modname)
        except ModuleNotFoundError:
            rt = _ShimRuntime(self._modname, self._summary, self._install_message)
            mod = rt.get(self._summary["root"])
        self._obj = mod
        self._loaded = True
        return mod

    def __getattr__(self, name: str) -> Any:
        obj = self._load()
        return getattr(obj, name)

    def __dir__(self) -> list[str]:
        if self._loaded and self._obj is not None:
            return sorted(set(dir(self._obj)))
        self._ensure_summary()
        assert self._summary is not None
        node = self._summary["nodes"][str(self._summary["root"])]
        return sorted(set(node.get("children", {}).keys()) | set(node.get("eager", [])))

    def __repr__(self) -> str:
        if not self._loaded:
            return f"<LazyModuleProxy for {self._stem!r}>"
        return repr(self._obj)
