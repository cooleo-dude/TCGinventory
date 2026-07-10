"""Guard: the domain and service layers must never import Qt (NFR-6 / M2 exit).

Scans the source of those two packages for any Qt import so the rule can't
silently regress.
"""

from __future__ import annotations

import pathlib

import tcginventory.domain
import tcginventory.services

_FORBIDDEN = ("PySide6", "PyQt6", "PyQt5")


def _package_py_files(package):
    root = pathlib.Path(package.__file__).parent
    return list(root.rglob("*.py"))


def test_domain_and_services_are_qt_free():
    offenders = []
    for package in (tcginventory.domain, tcginventory.services):
        for path in _package_py_files(package):
            text = path.read_text(encoding="utf-8")
            if any(token in text for token in _FORBIDDEN):
                offenders.append(str(path))
    assert offenders == [], f"Qt import found in headless layer: {offenders}"
