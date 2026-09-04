"""Headless Python backend foundations for Cortex."""

# The one place Cortex's version is written down. pyproject.toml reads it from
# here (see [tool.setuptools.dynamic]), the API reports it, the launcher stamps
# frontend builds with it, and a test holds frontend/package.json to it -- npm
# cannot read a Python constant, so equality there is enforced rather than
# derived.
__version__ = "0.1.0"
