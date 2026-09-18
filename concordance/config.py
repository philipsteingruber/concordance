"""Configuration, read from the process environment.

Deliberately not a dotenv parser: the caller sources its own credential file
(or systemd supplies an EnvironmentFile) before invoking us, so no secret path
is ever hard-coded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Percentage points subtracted from the CWA percentage Concordance writes. KOSync
# keeps the higher percentage across devices, and KOReader's own percentage
# (page-layout based) runs up to ~1 point below ours for the same spot. Writing
# ours as-is made CWA reject the Kobo's next genuine pushes until the reader
# passed the gap (found on a real device). The jump itself uses the XPointer, so
# a lower percentage only changes the number in KOReader's prompt.
CWA_PERCENT_MARGIN = float(os.environ.get("CONCORDANCE_CWA_PERCENT_MARGIN", "2.0"))


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


class ServiceUnavailable(RuntimeError):
    """A service didn't answer at all (down, restarting, wrong URL).

    Kept apart from other failures so a run that couldn't reach CWA or ABS is
    distinguishable from one that reached it and was refused: the first is
    routine while a container restarts, the second means something is wrong.
    """


def unreachable(exc: Exception) -> bool:
    """Whether a requests failure means "no answer" rather than "answered, badly"."""
    import requests
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Source your credential file into the environment "
            "before running (see README)."
        )
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def env_number(name: str, default: float, kind: type = float) -> float:
    """A numeric setting from the environment, failing with the variable's name."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return kind(default)
    try:
        return kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    cwa_url: str
    cwa_user: str
    cwa_password: str
    abs_url: str
    abs_api_key: str
    calibre_db: str
    calibre_root: str
    rewind_seconds: int
    # (path prefix as ABS reports it, the same directory on this host); None when unset.
    abs_audio_root: tuple[str, str] | None = None
    min_align_score: float = -1.0
    aligner_image: str = "concordance-aligner:latest"
    aligner_cpu_shares: int = 1024
    aligner_memory: str = "4g"

    @classmethod
    def from_env(cls) -> "Config":
        """Build config from the environment, failing loudly on anything missing."""
        try:
            rewind = int(_optional("CONCORDANCE_REWIND_SECONDS", "150"))
        except ValueError as exc:
            raise ConfigError("CONCORDANCE_REWIND_SECONDS must be an integer") from exc
        if rewind < 0:
            raise ConfigError("CONCORDANCE_REWIND_SECONDS must not be negative")

        audio_map = _optional("ABS_AUDIO_ROOT_MAP", "")
        if audio_map and "=" not in audio_map:
            raise ConfigError("ABS_AUDIO_ROOT_MAP must look like /path/in/abs=/path/on/host")
        try:
            cpu_shares = int(_optional("CONCORDANCE_ALIGNER_CPU_SHARES", "1024"))
            min_score = float(_optional("CONCORDANCE_MIN_ALIGN_SCORE", "-1.0"))
        except ValueError as exc:
            raise ConfigError(f"invalid number in configuration: {exc}") from exc
        calibre_root = _require("CALIBRE_ROOT").rstrip("/")

        return cls(
            cwa_url=_optional("CWA_URL", "http://localhost:8083").rstrip("/"),
            cwa_user=_require("CWA_USER"),
            cwa_password=_require("CWA_APP_PASSWORD"),
            abs_url=_optional("ABS_URL", "http://localhost:13378").rstrip("/"),
            abs_api_key=_require("ABS_API_KEY"),
            calibre_db=_optional("CALIBRE_DB", f"{calibre_root}/metadata.db"),
            calibre_root=calibre_root,
            # Bias CWA -> ABS writes backwards. Landing early costs a little
            # re-listening; landing late spoils plot and forces you to hunt
            # backwards through audio, which is much harder than skipping forward.
            rewind_seconds=rewind,
            # ABS reports audio paths as its own container sees them. Map that prefix to
            # the host directory, e.g. /audiobooks=/srv/media/audiobooks.
            abs_audio_root=tuple(audio_map.split("=", 1)) if audio_map else None,
            # Mean word log-probability below which an aligned position is not
            # trusted. Measured: a correct chapter scores about -0.1, text matched
            # to the wrong audio -3 or worse.
            min_align_score=min_score,
            aligner_image=_optional("CONCORDANCE_ALIGNER_IMAGE", "concordance-aligner:latest"),
            # Docker's default is 1024. Raise it to let alignment outrank other
            # containers (e.g. a transcoder) when both want the CPU.
            aligner_cpu_shares=cpu_shares,
            aligner_memory=_optional("CONCORDANCE_ALIGNER_MEMORY", "4g"),
        )
