#!/usr/bin/env python3
from pathlib import Path

path = Path("test_derivatives_ablation.py")
text = path.read_text(encoding="utf-8")
old = "import unittest\n\nfrom derivatives_ablation import eligible_training_rows, walk_forward_ablation\n"
new = (
    "import unittest\n\n"
    "from unit_test_stubs import install_timesfm_stub\n\n"
    "install_timesfm_stub()\n\n"
    "from derivatives_ablation import eligible_training_rows, walk_forward_ablation  # noqa: E402\n"
)
if old not in text:
    raise RuntimeError("expected derivatives ablation import block not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
