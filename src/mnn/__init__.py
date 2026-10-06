"""MNN: the Press, its sources, the builder and the staging tools.

The package is run from its repository (or from /app in the image), never
from site-packages, because the Press serves and reads files that sit beside
it: the Kindle's scripts and the default sources.toml."""

from pathlib import Path

# The repository root: src/mnn/__init__.py is two directories below it.
REPO = Path(__file__).resolve().parents[2]
