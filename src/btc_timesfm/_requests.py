"""Small compatibility wrapper for the optional requests dependency."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any


try:  # pragma: no cover - exercised only when requests is installed
    import requests as _requests
except Exception:  # pragma: no cover - import fallback for test environments

    class RequestException(Exception):
        pass

    def get(*args: Any, **kwargs: Any) -> Any:
        raise RequestException("requests is not available in this environment")

    _fallback = SimpleNamespace(get=get, RequestException=RequestException)

    class _RequestsProxy:
        def __getattr__(self, name: str) -> Any:
            module = sys.modules.get("requests", _fallback)
            return getattr(module, name)

    requests = _RequestsProxy()
else:
    requests = _requests
