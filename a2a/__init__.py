"""Compatibility shim: ``a2a`` was renamed to ``swarmly``.

Only for launchers written before the rename (``python -m a2a.telegram.cli``, the MCP
entries ``swarmly mcp-setup`` wrote as ``from a2a.swarm.cli import main``) that run from a
checkout of this repository. It is not packaged, so an install never puts a top-level
``a2a`` next to the Agent2Agent SDK's; it serves only checkouts that still use the old
names. Every ``a2a.<name>`` resolves to the same module object as ``swarmly.<name>``.

Re-point those launchers at ``swarmly`` and run ``pip install -e .`` again; this file goes
away in the next release.
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

import swarmly

_OLD, _NEW = __name__, "swarmly"


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        real = _NEW + fullname[len(_OLD):]
        real_spec = importlib.util.find_spec(real)
        if real_spec is None:
            return None
        spec = importlib.util.spec_from_loader(fullname, self,
                                               is_package=real_spec.submodule_search_locations is not None)
        spec.loader_state = real_spec
        return spec

    def create_module(self, spec):
        return importlib.import_module(spec.loader_state.name)

    def exec_module(self, module):
        # Already executed under its real name. The import machinery stamped the alias spec
        # on it; give the module back its own, so swarmly.<name> stays reloadable.
        module.__spec__ = module.__spec__.loader_state

    def get_code(self, fullname):
        # ``python -m a2a.x`` runs the real module's code as __main__.
        real = _NEW + fullname[len(_OLD):]
        return importlib.util.find_spec(real).loader.get_code(real)


if not any(isinstance(f, _Alias) for f in sys.meta_path):
    sys.meta_path.insert(0, _Alias())

# FutureWarning, not DeprecationWarning: shown by default, so a launcher's log says it.
warnings.warn("the a2a package was renamed to swarmly; update this launcher",
              FutureWarning, stacklevel=2)
__version__ = swarmly.__version__
__path__ = []  # a package, so ``a2a.<name>`` goes through the finder above
