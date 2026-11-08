"""How LOCKI sounds in each security mode: one voice, with the delivery changing. The security service decides when to speak and how urgently
(modes.py, _mode_changed); this only decides how a line sounds once it is spoken.

With security on, every line uses one local voice (Settings.security_voice, Kokoro): an urgent warning and the next reply must sound like the same
speaker, and neither may wait on the network. The mode changes speed (and, through runtime.style(), wording), never the speaker.

A security warning is delivered as the mode it belongs to, not whatever the mode is the instant it is spoken, so its clip can be rendered ahead
at exactly that speed and still play in milliseconds.
"""

from __future__ import annotations

NORMAL, WATCHFUL, DEFENSIVE, CRITICAL, OWNER_RETURNED = "NORMAL", "WATCHFUL", "DEFENSIVE", "CRITICAL", "OWNER_RETURNED"

# Kokoro speed per mode. None = the owner's own setting (voice_profile.kokoro_speed). Slower is firmer; CRITICAL stays clipped and urgent but clear.
MODE_SPEED: dict[str, float | None] = {NORMAL: None, WATCHFUL: 0.96, DEFENSIVE: 0.88, CRITICAL: 0.93, OWNER_RETURNED: None}

# The mode each security line is spoken in, whichever trigger fires it (the triggers are the security service's: see its _mode_changed and _warn).
CATEGORY_MODE: dict[str, str] = {
    "security_warning_presence": WATCHFUL,
    "security_camera_degraded": WATCHFUL,
    "security_warning_interaction": DEFENSIVE,
    "security_warning_app": DEFENSIVE,
    "security_warning_usb": CRITICAL,
    "security_warning_usb_unrecognized": CRITICAL,
    "security_warning_usb_person": CRITICAL,
    "security_warning_incident": CRITICAL,
    "security_warning_correlated": CRITICAL,
    "security_owner_seen": OWNER_RETURNED,
    "security_owner_seen_incident": OWNER_RETURNED,  # the usual voice even while an open incident keeps the mode CRITICAL
}


def mode_for(category: str, current_mode: str | None) -> str:
    return CATEGORY_MODE.get(category) or (current_mode if current_mode in MODE_SPEED else NORMAL)


def speed_for(category: str, current_mode: str | None) -> float | None:
    """The Kokoro speed for this line, or None for the owner's own setting."""
    return MODE_SPEED[mode_for(category, current_mode)]
