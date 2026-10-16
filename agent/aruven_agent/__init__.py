"""ARUVEN agent package.

Adds the repository root to ``sys.path`` so the shared ``aruven_common``
package (canonical secp256k1 + EIP-1559 signing) is importable when the agent
is run from ``agent/``.
"""
import pathlib as _pathlib
import sys as _sys

_REPO_ROOT = _pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

__version__ = "0.1.0"
