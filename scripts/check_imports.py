import importlib
import pkgutil

import app

modules = list(pkgutil.walk_packages(app.__path__, app.__name__ + "."))
for module in modules:
    importlib.import_module(module.name)
print(f"IMPORTS OK: {len(modules)} modules")
