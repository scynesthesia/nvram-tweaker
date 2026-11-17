"""Single source of truth for the version shown in the GUI, the CLI and the release check.

Bump this, commit, then tag the commit ``v<version>``; the release workflow refuses to
publish when the tag and this value disagree.
"""

__version__ = "2.0.0-dev"
