"""Read/write the local identity profile at ~/.reachify/.profile.

The profile is stored as JSON and holds the identity ``id`` and ``identity_token``
that the CLI uses to authenticate against the backend.
"""

from __future__ import annotations

import json
import stat

from .config import PROFILE_DIR, PROFILE_PATH
from .models import Profile


class ProfileError(Exception):
    """Raised when the profile is missing or malformed."""


def load_profile() -> Profile:
    """Load and parse ~/.reachify/.profile.

    Raises:
        ProfileError: if the file is missing or cannot be parsed.
    """
    if not PROFILE_PATH.exists():
        raise ProfileError(
            f"No profile found at {PROFILE_PATH}. Run `reachify login` first."
        )
    try:
        raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Could not read profile at {PROFILE_PATH}: {exc}") from exc

    try:
        return Profile.from_dict(raw)
    except KeyError as exc:
        raise ProfileError(f"Profile at {PROFILE_PATH} is missing field {exc}.") from exc


def save_profile(profile: Profile) -> None:
    """Persist a profile to ~/.reachify/.profile with 0600 permissions."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": profile.id,
        "identity_token": profile.identity_token,
    }
    if profile.api_base_url:
        payload["api_base_url"] = profile.api_base_url

    PROFILE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Token material — restrict to owner read/write only.
    PROFILE_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
