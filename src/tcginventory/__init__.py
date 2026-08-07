"""TCGinventory — local desktop trading-card inventory tracker."""

#: The one place the version is written. `pyproject.toml` reads this literal via
#: `[tool.setuptools.dynamic]`, so packaging metadata, the window title, the
#: splash and the frozen exe can never disagree. Bump it with `/sdd-version`.
__version__ = "0.7.0"
