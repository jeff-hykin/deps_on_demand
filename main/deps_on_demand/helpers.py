import importlib
import pkgutil

def submodules_requiring_import(modname: str) -> list[str]:
    """
    List fully-qualified submodules under `modname` that are not already
    accessible as attributes after importing `modname` itself.
    """
    root = importlib.import_module(modname)
    if not hasattr(root, "__path__"):
        return []  # not a package, no submodules

    base_parts = modname.split(".")
    need_import: list[str] = []

    for info in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
        full = info.name
        # Determine the attribute path relative to the root module
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