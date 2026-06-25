"""Paths and constants for the Reachify CLI."""

from __future__ import annotations

import os
from pathlib import Path

#: Default backend base URL. Resolution order (first wins):
#:   1. ``--api-base-url`` flag saved in the profile (``Profile.api_base_url``)
#:   2. ``REACHIFY_API_BASE_URL`` environment variable
#:   3. this default
DEFAULT_API_BASE_URL = os.environ.get("REACHIFY_API_BASE_URL", "https://api.reachifie.com")

#: Path to the profile file: ~/.reachify/.profile
PROFILE_DIR = Path.home() / ".reachify"
PROFILE_PATH = PROFILE_DIR / ".profile"

#: Fallback workspace root, used only when a job omits ``workspace.work_dir``.
#: The backend normally dictates absolute paths; this is the safety net.
#: Overridable via REACHIFY_WORKSPACE_ROOT.
WORKSPACE_ROOT = Path("/tmp") / ".reachify"
