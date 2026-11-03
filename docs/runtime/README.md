# Optional runtime tools

The content-addressed uv wheel is an unmodified copy of the upstream PyPI artifact.
It is used only after the user requests a local AI environment, never during
basic app startup. Its SHA-256 and executable SHA-256 are pinned in the signed app.
The wheel includes its upstream MIT/Apache-2.0 licenses. Sources:
https://github.com/astral-sh/uv/tree/0.9.25
https://pypi.org/project/uv/0.9.25/
