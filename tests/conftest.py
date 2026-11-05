"""Shared fixtures for the widget render tests."""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(scope="session")
def node_available():
    """The node binary, or None when this machine has none (the render
    tests skip in that one case; see tests/test_render.py's docstring —
    dependency-wise the suite has nothing to skip on)."""
    for candidate in ("node", "/usr/bin/node"):
        try:
            proc = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            if proc.returncode == 0:
                return candidate
        except OSError:
            continue
    return None
