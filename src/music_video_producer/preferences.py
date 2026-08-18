"""Preferences that describe *this machine*, not any one video.

There is exactly one of these today: whether the language model is ejected before a ComfyUI
submission. It is here rather than on `Project` for a reason that is worth stating plainly —
a project manifest is a shareable document, and a manifest carrying "do not eject" would
silently change how someone else's renders behave on hardware it knows nothing about. The
file therefore sits *beside* `projects/` rather than inside any project directory, and
nothing in `models.py` has a field for it.

Two rules mirror `vram.py`'s parser, for the same reason:

* An unreadable or absent file yields "nothing was chosen", never a guessed value. The
  caller distinguishes "no preference" from `False` and falls back to the configured
  default, so a corrupt file cannot silently switch a feature off.
* A value of the wrong type is not coerced. `"false"` is not `False` here; it is not a
  choice this file understands, and pretending otherwise is how a typo becomes a setting.

Reads are per call rather than cached: the file is small and local, and a cached copy would
disagree with the disk after any hand edit.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

logger = logging.getLogger(__name__)

#: Sibling of `projects/`, never inside it. The name says what the file is for, so a
#: Director who finds it in a backup is not left guessing whether it belongs to a project.
PREFERENCES_FILENAME = "machine-preferences.json"

#: The one key stored today. Named for the setting it mirrors so the file, the environment
#: variable and the field on `Settings` are recognisably the same thing.
EJECT_PREFERENCE_KEY = "llm_eject_before_render"


class MachinePreferences:
    """Machine-scoped preferences, stored as one small JSON object under the data root."""

    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root) / PREFERENCES_FILENAME

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            # Not fatal and not silent. Nothing here is worth failing a startup over, but a
            # preference that is being ignored has to say so somewhere.
            logger.warning("Ignoring unreadable machine preferences at %s: %s", self.path, error)
            return {}
        return payload if isinstance(payload, dict) else {}

    def get_bool(self, key: str) -> bool | None:
        """The stored choice, or None when there is none this file can read.

        None is not `False`. The caller has a configured default and an environment variable
        to fall back to, and collapsing "never chosen" into "chosen off" would let a missing
        file turn a defaulted-on feature off.
        """
        value = self._read().get(key)
        return value if isinstance(value, bool) else None

    def set_bool(self, key: str, value: bool) -> None:
        """Store one choice, atomically, preserving every other key already in the file.

        Read-modify-write rather than overwrite, so adding a second preference later cannot
        make saving the first one delete it. Raises `OSError` if the write fails — the caller
        refuses the change rather than reporting a setting that would not survive a restart.
        """
        payload = self._read()
        payload[key] = bool(value)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as temp:
            json.dump(payload, temp, indent=2)
            temp.flush()
            os.fsync(temp.fileno())
            temporary_path = Path(temp.name)
        temporary_path.replace(self.path)
