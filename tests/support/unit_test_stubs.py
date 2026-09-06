"""Small import stubs used by unit tests.

The production package imports ``timesfm3`` at module import time, but unit tests
exercise only the project's pure Python logic. Keeping a tiny stub here avoids
installing the multi-gigabyte Torch/TimesFM stack in the unit-test workflow.
"""

from __future__ import annotations

import sys
import types
from typing import Any


def install_timesfm_stub() -> None:
    if "timesfm3" in sys.modules:
        pass

    if "requests" not in sys.modules:
        requests_module = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        def get(*args: Any, **kwargs: Any) -> Any:
            raise RequestException("requests is stubbed in unit tests")

        requests_module.RequestException = RequestException
        requests_module.get = get
        sys.modules["requests"] = requests_module

    if "timesfm3" in sys.modules:
        return

    module = types.ModuleType("timesfm3")

    class ModelConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class TimesFM3Evaluator:
        def __init__(self, config: ModelConfig | None = None) -> None:
            self.config = config

    module.ModelConfig = ModelConfig
    module.TimesFM3Evaluator = TimesFM3Evaluator
    sys.modules["timesfm3"] = module
