"""Personality: how LOCKED IN says things. Never what happened.

Pipeline: event -> category (chosen by the runtime / policy from a recorded event) ->
response intent (``INTENTS``) -> personality rendering (this module) -> speech queue.

* Modes: chill, roast, unhinged (explicit opt-in), judge_demo. A fifth pool, "plain",
  serves the "be serious" tone and the safety acknowledgements.
* Profanity: off / mild / strong, chosen by the user, capped by the mode (judge demo is
  always clean, chill at most mild). Every line carries its measured level.
* Insults aimed at the user exist only in unhinged mode, only after opting in, never in
  the minutes after the user asked for help, and are paced (never twice in a row, at
  most one per ``INSULT_GAP_S``).
* Lines are filled only from recorded facts; a line that needs a fact we do not have is
  skipped, so a joke can never invent a number or an event.
* ``cue`` is an optional delivery hint for Fish Audio S2 voices ("[excited] ..."); it is
  never shown and never spoken by the local voice.
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .content_guard import check as guard_check

MODES = ("chill", "roast", "unhinged", "judge_demo")
MODE_LABELS = {"chill": "Chill", "roast": "Roast", "unhinged": "Unhinged", "judge_demo": "Judge demo"}
PROFANITY = ("off", "mild", "strong")
PROFANITY_LEVEL = {"off": 0, "mild": 1, "strong": 2}
MODE_PROFANITY_CAP = {"chill": 1, "roast": 2, "unhinged": 2, "judge_demo": 0}
TONES = ("normal", "gentle", "serious")
INSULT_GAP_S = 90.0
# Jokes land on timing. Fixed lines play from a pre-rendered clip in ~10 ms; a line with a
# live number must be synthesised first (~2 s with the local voice). So reactions use fixed
# lines most of the time and a line with a number now and then for variety.
STATIC_JOKE_SHARE = 0.8
INSULT_MIN_LINES_BETWEEN = 2
SUPPORTIVE_S = 300.0

P0, P1, P2, P3 = 0, 1, 2, 3


@dataclass(frozen=True)
class Intent:
    name: str
    priority: int
    joke: bool = False  # suppressed entirely in the serious tone if no plain line exists


# category -> response intent. The category always comes from a recorded event or command.
INTENTS: dict[str, Intent] = {
    "security_warning_usb": Intent("safety_ack", P0),
    "security_warning_app": Intent("safety_ack", P0),
    "security_warning_incident": Intent("safety_ack", P0),
    "security_warning_correlated": Intent("safety_ack", P0),
    "security_warning_presence": Intent("safety_ack", P0),
    "security_warning_interaction": Intent("safety_ack", P0),
    "security_warning_usb_unrecognized": Intent("safety_ack", P0),
    "security_warning_usb_person": Intent("safety_ack", P0),
    "security_owner_seen": Intent("status_update", P1),
    # Demo mode (security/demo.py): one line per step the presenter takes.
    "security_owner_seen_incident": Intent("status_update", P1),
    "security_demo_started": Intent("status_update", P1),
    "security_demo_enroll_start": Intent("status_update", P1),
    "security_demo_enrolled": Intent("status_update", P1),
    "security_demo_recognized": Intent("status_update", P1),
    "security_demo_ended": Intent("status_update", P1),
    "security_disarm_on_dashboard": Intent("status_update", P1),
    "security_camera_degraded": Intent("status_update", P1),
    "security_armed_camera": Intent("status_update", P1),
    "security_arm_on_dashboard": Intent("status_update", P1),
    "quiet_mode_on": Intent("status_update", P1),
    "security_arm_needs_grant": Intent("status_update", P1),
    "security_arm_refused": Intent("status_update", P1),
    "security_arm_unreachable": Intent("status_update", P1),
    "security_arm_unconfirmed": Intent("status_update", P1),
    "security_armed": Intent("status_update", P1),
    "security_armed_degraded": Intent("status_update", P1),
    "security_already_armed": Intent("status_update", P1),
    "security_not_armed": Intent("status_update", P1),
    "security_confirm_arm": Intent("status_update", P1),
    "security_disarm_needs_owner": Intent("status_update", P1),
    "security_disarmed": Intent("status_update", P1),
    "security_expired": Intent("status_update", P1),
    "security_lock_requested": Intent("status_update", P1),
    "security_lock_confirmed": Intent("status_update", P1),
    "security_lock_failed": Intent("status_update", P1),
    "security_lock_dry_run": Intent("status_update", P1),
    "security_checking": Intent("status_update", P1),
    "security_report": Intent("status_update", P2),
    "mission_planning": Intent("acknowledgement", P2),
    "mission_looking": Intent("acknowledgement", P2),
    "mission_looking_near": Intent("acknowledgement", P2),
    "mission_read_no_answer": Intent("acknowledgement", P2),
    "mission_model_slow": Intent("acknowledgement", P2),
    "mission_tool_failed": Intent("acknowledgement", P2),
    "mission_tool_refused": Intent("acknowledgement", P2),
    "mission_start": Intent("energetic_intro", P2),
    "mission_no_boss": Intent("acknowledgement", P2),
    "mission_already": Intent("acknowledgement", P2),
    "mission_none": Intent("acknowledgement", P2),
    "mission_tests_wont_run": Intent("acknowledgement", P2),
    "mission_tests_started": Intent("acknowledgement", P2),
    "mission_status": Intent("acknowledgement", P2),
    "mission_hint": Intent("acknowledgement", P2),
    "mission_stuck": Intent("acknowledgement", P2),
    "mission_ended": Intent("acknowledgement", P2),
    "mission_edit_ready": Intent("acknowledgement", P2),
    "mission_edit_failed": Intent("acknowledgement", P2),
    "boss_hit": Intent("progress_praise", P2),
    "checkpoint_done": Intent("progress_praise", P2),
    "boss_beaten": Intent("celebration", P1),
    "boss_healed": Intent("playful_warning", P2),
    "mission_regression": Intent("playful_warning", P2),
    "tests_edited": Intent("acknowledgement", P2),
    "code_broken": Intent("acknowledgement", P2),
    "tests_timeout": Intent("acknowledgement", P2),
    "reach_mission": Intent("playful_warning", P2, True),
    "session_start": Intent("energetic_intro", P2),
    "reach_early": Intent("playful_warning", P3, True),
    "reach_first": Intent("playful_warning", P3, True),
    "reach_repeat": Intent("playful_warning", P3, True),
    "reach_repeat_many": Intent("playful_warning", P3, True),
    "reach_holding": Intent("playful_warning", P3, True),
    "progress_ack": Intent("progress_praise", P2),
    "target_reached": Intent("celebration", P2),
    "end_completed": Intent("celebration", P1),
    "end_early": Intent("acknowledgement", P2),
    "pause": Intent("acknowledgement", P2),
    "resume": Intent("acknowledgement", P2),
    "release": Intent("release_ack", P0),
    "stop_ack": Intent("safety_ack", P0),
    "estop_ack": Intent("safety_ack", P0),
    "unmute": Intent("acknowledgement", P2),
    "harder": Intent("acknowledgement", P2),
    "tone_down": Intent("acknowledgement", P2),
    "serious_ack": Intent("acknowledgement", P2),
    "tone_reset": Intent("acknowledgement", P2),
    "personality_set": Intent("acknowledgement", P2),
    "profanity_off": Intent("acknowledgement", P2),
    "profanity_mild": Intent("acknowledgement", P2),
    "profanity_strong": Intent("acknowledgement", P2),
    "unhinged_needs_opt_in": Intent("acknowledgement", P2),
    "help": Intent("helpful_explanation", P2),
    "unknown": Intent("clarify", P2),
    "low_confidence": Intent("clarify", P2),
    "nudge_distracted": Intent("nudge", P3, True),
    "welcome_back": Intent("nudge", P3, True),
    "milestone_halfway": Intent("status_update", P1),
    "milestone_five_left": Intent("status_update", P1),
    "already_active": Intent("acknowledgement", P2),
    "no_session": Intent("acknowledgement", P2),
    "not_paused": Intent("acknowledgement", P2),
    "return_ack": Intent("acknowledgement", P2),
    "return_refused": Intent("status_update", P1),
    "greeting": Intent("small_talk", P2),
    "thanks": Intent("small_talk", P2),
    "status": Intent("session_metrics", P2),
    "time_left": Intent("session_metrics", P2),
    "next_step": Intent("session_metrics", P2),
    "voice_test": Intent("acknowledgement", P2),
    "retreat_blocked": Intent("robot_failure", P3, True),
    "robot_lost": Intent("robot_failure", P1),
    "robot_back": Intent("robot_recovery", P2),
    "camera_lost": Intent("robot_failure", P1),
    "focus_streak": Intent("progress_praise", P3),
    "reach_warning": Intent("playful_warning", P3, True),
    "roast_me": Intent("acknowledgement", P2),
    "about": Intent("small_talk", P2),
    # Milestone 5: fixed fallbacks for the actions the local language model can request, used
    # when its own words were not spoken (refused, too slow, or the model is off).
    "quiet_ack": Intent("acknowledgement", P2),
    "goal_set": Intent("acknowledgement", P2),
    "extended": Intent("acknowledgement", P2),
    "ack": Intent("acknowledgement", P2),
    "led_unsupported": Intent("status_update", P2),
    "camera_back": Intent("robot_recovery", P2),
    "cant_do": Intent("acknowledgement", P2),
    "camera_off_ack": Intent("acknowledgement", P2),
    "camera_on_ack": Intent("acknowledgement", P2),
    "mic_off_ack": Intent("acknowledgement", P2),
    "no_answer": Intent("clarify", P2),
}

ORDINALS = {2: "second", 3: "third", 4: "fourth", 5: "fifth"}
_MODE_CODES = {"C": "chill", "R": "roast", "U": "unhinged", "J": "judge_demo", "P": "plain"}


@dataclass(frozen=True)
class Line:
    text: str
    modes: frozenset[str]
    prof: int = 0
    insult: bool = False
    cue: str | None = None

    @property
    def slots(self) -> list[str]:
        return re.findall(r"{(\w+)}", self.text)


def L(text: str, modes: str, prof: int = 0, insult: bool = False, cue: str | None = None) -> Line:  # noqa: N802
    return Line(text, frozenset(_MODE_CODES[c] for c in modes), prof, insult, cue)


ALL = "CRUJP"

LIBRARY: dict[str, list[Line]] = {
    "session_start": [
        L("{planned} on {task}. I've got your phone, you've got this.", "C", cue="warm"),
        L("Okay, {planned}. Ease in, I'll hold the phone.", "C", cue="calm"),
        L("Okay. Ease in, I'll hold the phone.", "C", cue="calm"),
        L("Okay. Phone's mine now. Go.", "R", cue="confident"),
        L("{planned} on {task}. Let's see if you actually finish something.", "RU", cue="teasing"),
        L("{planned}. Phone's with me. Go.", "R", cue="confident"),
        L("Alright, lock the fuck in.", "RU", prof=2, cue="hyped"),
        L("{planned}. Get your ass in gear.", "RU", prof=1, cue="teasing"),
        L("{planned} on {task}. Don't make me come over there.", "U", cue="smug"),
        L("Phone's mine. Don't make me come over there.", "U", cue="smug"),
        L("{planned}. Touch that phone and I'm telling everyone, dumbass.", "U", prof=1, insult=True, cue="smug"),
        L("{planned} on {task}! Phone secured. Brain, engage.", "J", cue="energetic"),
        L("Phone secured. Focus mode on. I'm watching.", "J", cue="playful"),
        L("Session started: {planned} on {task}. Phone locked away.", "P"),
    ],
    "reach_early": [
        L("Already? We just started.", "C", cue="amused"),
        L("Easy. It's only been {elapsed}.", "C", cue="calm"),
        L("Bro, we literally just started.", "R", cue="exasperated"),
        L("Bro. Already?", "RU", cue="deadpan"),
        L("It's been {elapsed}. Put it down.", "R", cue="teasing"),
        L("Get your ass back to work.", "RU", prof=1, cue="exasperated"),
        L("We are {elapsed} in. Are you shitting me?", "RU", prof=2, cue="exasperated"),
        L("We are {elapsed} in. Absolutely not.", "U", cue="deadpan"),
        L("Dumbass, we just started.", "U", prof=1, insult=True, cue="exasperated"),
        L("Dumb as shit move. We literally just started.", "U", prof=2, insult=True, cue="exasperated"),
        L("Whoa, already? We just started!", "J", cue="surprised"),
        L("Not yet! The phone stays with me.", "J", cue="playful"),
        L("Phone reach detected early in the session.", "P"),
    ],
    "reach_first": [
        L("Nice try.", "CR", cue="amused"),
        L("I saw that. {remaining} to go.", "C", cue="calm"),
        L("I saw that.", "R", cue="teasing"),
        L("Your phone can survive {remaining} without you.", "R", cue="teasing"),
        L("Get your ass back to work.", "RU", prof=1, cue="exasperated"),
        L("I saw that. Everyone saw that.", "U", cue="smug"),
        L("Hands off the damn phone.", "U", prof=1, cue="annoyed"),
        L("Put the fucking phone down.", "U", prof=2, cue="annoyed"),
        L("Nice try, dumbass.", "U", prof=1, insult=True, cue="laughing"),
        L("Caught you! {remaining} to go.", "J", cue="playful"),
        L("Nope! Back to {task}.", "J", cue="energetic"),
        L("Nope! Not today!", "J", cue="playful"),
        L("Caught you! The phone stays with me.", "J", cue="energetic"),
        L("Phone reach detected. {remaining} left.", "P"),
    ],
    "reach_repeat": [
        L("Again? Phone's fine, I promise.", "C", cue="warm"),
        L("That's {reaches}. Hang in there.", "C", cue="calm"),
        L("That's your {attempt_ordinal} attempt. Bold.", "RU", cue="smug"),
        L("I saw that. Again.", "RU", cue="deadpan"),
        L("Again? Really? Again?", "U", cue="exasperated"),
        L("{reaches}. Seriously, get your ass back to work.", "R", prof=1, cue="exasperated"),
        L("That's {reaches}. What the hell are we doing?", "RU", prof=1, cue="exasperated"),
        L("Dumbass, that's your {attempt_ordinal} attempt.", "U", prof=1, insult=True, cue="exasperated"),
        L("Your {attempt_ordinal} attempt. Dumb as shit, but consistent.", "U", prof=2, insult=True, cue="laughing"),
        L("That's attempt number {reaches_n}! The phone is staying with me.", "J", cue="playful"),
        L("{reaches}! The phone has fans, I see.", "J", cue="playful"),
        L("Again?! Bold strategy.", "J", cue="surprised"),
        L("Phone reach number {reaches_n}.", "P"),
    ],
    "reach_repeat_many": [
        L("That's {reaches}. Want to take a real break instead?", "C", cue="warm"),
        L("Want to take a real break instead?", "C", cue="warm"),
        L("The phone is not the assignment.", "RU", cue="deadpan"),
        L("{reaches}. The phone is not the assignment.", "RU", cue="deadpan"),
        L("{reaches}. Say 'pause' if you need a real break.", "R", cue="calm"),
        L("{reaches}. This is some bullshit, my guy.", "RU", prof=2, cue="exasperated"),
        L("{reaches}. At this point the phone should study for you.", "U", cue="smug"),
        L("{reaches}. You absolute dumbass. Back to work.", "U", prof=1, insult=True, cue="exasperated"),
        L("{reaches}! Maybe it's time for a quick break?", "J", cue="friendly"),
        L("Maybe it's time for a quick break?", "J", cue="friendly"),
        L("{reaches} so far. Consider a short break.", "P"),
    ],
    "reach_holding": [
        L("It's over here now.", "C", cue="calm"),
        L("It's over here now. Nice try.", "R", cue="teasing"),
        L("Reaching for an empty spot. Iconic.", "RU", cue="laughing"),
        L("That's empty air, genius.", "U", insult=True, cue="smug"),
        L("The phone's safe with me over here!", "J", cue="playful"),
        L("The phone is already with the robot.", "P"),
    ],
    "progress_ack": [
        L("Nice. That's {completed_phrase}.", "C", cue="warm"),
        L("Got it. {completed_phrase}.", "C", cue="calm"),
        L("Damn, nice. {completed_phrase}.", "C", prof=1, cue="impressed"),
        L("Damn, nice. Keep going.", "C", prof=1, cue="impressed"),
        L("Okay, that was actually clean.", "RU", cue="impressed"),
        L("You're cooking. Keep going.", "R", cue="hyped"),
        L("{completed_phrase}. {units_left_sentence}", "R", cue="confident"),
        L("Damn, okay. {completed_phrase}.", "RU", prof=1, cue="impressed"),
        L("{completed_phrase}. Look at you go.", "U", cue="teasing"),
        L("Holy shit, you're actually cooking.", "U", prof=2, cue="excited"),
        L("{completed_phrase}! Actual productivity, live on stage!", "J", cue="excited"),
        L("Look at that. Real progress.", "J", cue="impressed"),
        L("Yes! {units_left_sentence}", "J", cue="excited"),
        L("Logged. {completed_phrase}.", "P"),
    ],
    "target_reached": [
        L("That's the target. {completed_phrase}. Nice work.", "C", cue="warm"),
        L("That's what I'm talking about!", "RJ", cue="very excited"),
        L("Target hit. You actually did it.", "R", cue="impressed"),
        L("HOLY SHIT, YOU ACTUALLY DID IT.", "RU", prof=2, cue="very excited"),
        L("You did it. I'm genuinely shocked. Proud, but shocked.", "U", cue="laughing"),
        L("Target reached! {completed_phrase}!", "J", cue="very excited"),
        L("Target reached: {completed_phrase}.", "P"),
    ],
    "end_completed": [
        L("That's time. Nice work. {summary}", "C", cue="warm"),
        L("Time. {summary} That's what I'm talking about!", "R", cue="excited"),
        L("Okay, you earned a break. {summary}", "RC", cue="warm"),
        L("Time's up and you didn't fold. Hell yes.", "RU", prof=1, cue="excited"),
        L("Time's up. Holy shit, you actually locked in.", "RU", prof=2, cue="very excited"),
        L("Time's up. {summary} Go touch grass.", "U", cue="teasing"),
        L("And that's time! {summary} Great session!", "J", cue="very excited"),
        L("Session complete. {summary}", "P"),
    ],
    "end_early": [
        L("Session ended. {summary}", "C"),
        L("Ending early? {summary} I'll allow it.", "R", cue="teasing"),
        L("Bailing early? {summary} Fine.", "U", cue="deadpan"),
        L("Session ended. {summary} Nice effort!", "J", cue="friendly"),
        L("Session ended early. {summary}", "P"),
    ],
    "pause": [
        L("Paused. Take your time.", "C", cue="calm"),
        L("Paused. Timer's frozen at {remaining} left.", "R"),
        L("Paused. Don't get comfortable.", "R", cue="teasing"),
        L("Paused. The timer misses you already.", "U", cue="teasing"),
        L("Paused! Take a breather.", "J", cue="friendly"),
        L("Paused with {remaining} left.", "P"),
    ],
    "resume": [
        L("Back at it. {remaining} left.", "C", cue="warm"),
        L("Back at it.", "C", cue="warm"),
        L("Back to it. Lock in.", "R", cue="confident"),
        L("Break's over. Move.", "U", cue="stern"),
        L("And we're back!", "J", cue="energetic"),
        L("Resumed. {remaining} left. Lock in.", "R", cue="confident"),
        L("Resumed. {remaining}. Lock the fuck in.", "RU", prof=2, cue="hyped"),
        L("Resumed. {remaining}. No excuses.", "U", cue="stern"),
        L("We're back! {remaining} to go!", "J", cue="energetic"),
        L("Resumed. {remaining} left.", "P"),
        # Same text and cue as the cached welcome_back lines, so they play in the cloud voice offline.
        L("Oh, you're back. Good.", "R", cue="teasing"),
        L("Oh, look who's back.", "U", cue="teasing"),
        L("There you are!", "J", cue="friendly"),
    ],
    "release": [
        L("All yours.", "C"),
        L("Okay, phone's free.", "C"),
        L("Fine. Robot privileges revoked.", "R"),
        L("Fine. You win this round.", "RU"),
        L("Fine. Take your precious phone.", "U"),
        L("Phone released! Thanks for focusing.", "J"),
        L("Phone released. Session ended.", "P"),
    ],
    "retreat_blocked": [
        L("I'd back away, but I'm not allowed to move right now. Consider this a stern look.", "CRJ", cue="deadpan"),
        L("Wheels are locked. I'm judging you from right here.", "RU", cue="smug"),
        L("Can't move. Doesn't matter. I saw that.", "RU", cue="deadpan"),
        L("I'm not allowed to move, which is the only reason you still have that phone.", "U", cue="annoyed"),
        L("I can't move right now, so this is just a very disappointed beep.", "CJ", cue="sad"),
        L("Phone reach detected. The robot is not armed to move.", "P"),
    ],
    "robot_lost": [
        L("Lost my legs for a second. Don't get ideas.", "CRJ", cue="worried"),
        L("My wheels just dropped off the network. Stay put.", "RU", cue="annoyed"),
        L("Connection to the robot lost. It can't move until it's back.", "P"),
    ],
    "robot_back": [
        L("I'm back. Did you touch the phone? Don't answer that.", "RU", cue="suspicious"),
        L("Back online. Missed me?", "CJ", cue="cheerful"),
        L("Robot connection restored. It needs to be armed again.", "P"),
    ],
    "camera_lost": [
        L("My eyes just went dark. I'm choosing to trust you. Big mistake.", "RU", cue="dramatic"),
        L("I can't see you right now. Honor system, okay?", "CJ", cue="worried"),
        L("Camera lost. Phone reaches can't be detected until it's back.", "P"),
    ],
    "stop_ack": [L("Stopped.", ALL)],
    "estop_ack": [L("Emergency stop. I'm not moving.", ALL)],
    "unmute": [L("I'm back.", "CRUP"), L("I'm back!", "J")],
    "harder": [
        L("Okay, stricter mode.", "C"),
        L("Stricter it is.", "R"),
        L("Oh, you want smoke? Fine.", "U", cue="smug"),
        L("Turning up the intensity!", "J", cue="energetic"),
        L("Strict mode on.", "P"),
    ],
    "tone_down": [L("Okay. No more roasting. I'll be nice.", "C", cue="warm"), L("Understood. No more jokes at your expense.", "P")],
    "serious_ack": [L("Okay. Serious mode. Just the facts.", ALL)],
    "tone_reset": [
        L("Okay, jokes are back.", "C"),
        L("Oh, we're back. Good.", "R", cue="smug"),
        L("Unleashed again. You asked for this.", "U", cue="smug"),
        L("Fun mode is back!", "J", cue="energetic"),
        L("Normal mode.", "P"),
    ],
    "personality_set": [
        L("Chill mode. Deep breaths.", "C", cue="calm"),
        L("Roast mode. You asked for it.", "R", cue="smug"),
        L("Unhinged mode. Oh, this is gonna be fun.", "U", cue="laughing"),
        L("Judge demo mode! Best behavior.", "J", cue="energetic"),
        L("{new_personality} mode.", "P"),
    ],
    "profanity_off": [L("Clean language only.", ALL)],
    "profanity_mild": [L("Okay, a little swearing allowed.", ALL)],
    "profanity_strong": [L("Gloves off. Language unlocked.", ALL)],
    "unhinged_needs_opt_in": [L("Unhinged mode needs your opt-in in the dashboard first.", ALL)],
    "help": [
        L("Okay, let's slow down. What's the very next small step on {task}?", "C", cue="warm"),
        L("No stress. Pick the smallest piece of {task} and do just that.", "C", cue="warm"),
        L("Alright, no roasting. Break {task} into the next tiny step and start there.", "R", cue="warm"),
        L("Real talk: write down exactly where you're stuck, then try one small thing.", "RU", cue="warm"),
        L("Okay, jokes off. Pick one tiny part of {task} and just start it.", "U", cue="warm"),
        L("Okay, tiny steps. What's the smallest piece of {task} you can do right now?", "J", cue="warm"),
        L("Try breaking {task} into one small next step. Say 'pause' if you need a real break.", "P"),
    ],
    "unknown": [
        L("Didn't catch that.", "C"),
        L("Say that again?", "R"),
        L("What? Say it like a normal person.", "U", cue="deadpan"),
        L("Say that one more time?", "J"),
        L("I didn't understand that.", "P"),
    ],
    "low_confidence": [L("I'm not sure I heard that right.", ALL)],
    "nudge_distracted": [
        L("Hey. {task} is still here.", "C", cue="calm"),
        L("Hey. Eyes back on the work.", "C", cue="calm"),
        L("Eyes back on {task}?", "R", cue="teasing"),
        L("Eyes back on the work?", "R", cue="teasing"),
        L("Hello? Your work called. It misses you.", "U", cue="sarcastic"),
        L("Psst. The work misses you.", "J", cue="whispering"),
        L("Hey. {remaining} left on {task}.", "R"),
        L("Hey. Get your ass back to {task}.", "RU", prof=1, cue="teasing"),
        L("Hello? {task} called. It misses you.", "U", cue="sarcastic"),
        L("Let's get back to {task}!", "J", cue="friendly"),
        L("Focus reminder: {task}.", "P"),
    ],
    "welcome_back": [
        L("Welcome back.", "C", cue="warm"),
        L("Welcome back. {remaining} left.", "R"),
        L("Oh, you're back. Good.", "R", cue="teasing"),
        L("Oh, you're back. {remaining} left.", "U", cue="teasing"),
        L("Oh, look who's back.", "U", cue="teasing"),
        L("Welcome back! {remaining} to go!", "J", cue="friendly"),
        L("There you are!", "J", cue="friendly"),
        L("Welcome back. {remaining} left.", "P"),
    ],
    "milestone_halfway": [
        L("Halfway there.", "C", cue="calm"),
        L("Halfway. {remaining} left.", "R"),
        L("Halfway. Don't fold now.", "U", cue="stern"),
        L("Halfway there! Keep it up!", "J", cue="energetic"),
        L("Halfway point. {remaining} left.", "P"),
    ],
    "milestone_five_left": [
        L("Five minutes left.", "CP"),
        L("Five minutes left. Finish strong.", "R", cue="confident"),
        L("Five minutes. Sprint.", "U", cue="hyped"),
        L("Five minutes. Haul ass.", "U", prof=1, cue="hyped"),
        L("Five minutes left! Finish strong!", "J", cue="energetic"),
    ],
    "already_active": [L("We're already locked in. {remaining} left.", ALL)],
    "no_session": [L("No session running. Say 'lock in' to start one.", ALL)],
    "not_paused": [L("We're not paused.", ALL)],
    "return_ack": [L("Coming back.", ALL)],
    "return_refused": [L("I can't move right now: {reason}.", ALL)],
    "greeting": [
        L("Hey.", "CP"),
        L("Hey. Ready to lock in?", "R"),
        L("Yo. Phone. Now. Kidding. Mostly.", "U", cue="playful"),
        L("Hey! I'm LOCKED IN. Your phone is in good hands. Mine.", "J", cue="playful"),
    ],
    "thanks": [L("Anytime.", "CRP"), L("Yeah, yeah. Get back to work.", "U", cue="deadpan"), L("Anytime. Now back to work!", "J")],
    "voice_test": [
        L("Hey, it's LOCKED IN. This is how I sound.", "CP"),
        L("LOCKED IN, voice check. Your phone is mine now.", "R", cue="smug"),
        L("Voice check. Touch that phone and see what happens.", "U", cue="smug"),
        L("Hi judges! I'm LOCKED IN, and I'm keeping this phone safe.", "J", cue="energetic"),
    ],
    # Milestone 4 reactions. The first two focus_streak lines are cached progress_ack clips.
    "focus_streak": [
        L("Okay, that was actually clean.", "RU", cue="impressed"),
        L("Look at that. Real progress.", "J", cue="impressed"),
        L("{streak} without touching the phone. Who even are you?", "RU", cue="impressed"),
        L("Still locked in. Nice.", "C", cue="warm"),
        L("{streak} of focus. Nice.", "C", cue="warm"),
        L("{streak} locked in. Look at that focus!", "J", cue="excited"),
        L("{streak} without a reach.", "P"),
    ],
    "reach_warning": [
        L("I see that hand.", "RU", cue="suspicious"),
        L("Don't even think about it.", "R", cue="warning"),
        L("Touch it. I dare you.", "U", cue="smug"),
        L("Careful...", "J", cue="playful"),
        L("Hands where I can see them.", "C", cue="playful"),
        L("Hand near the phone.", "P"),
    ],
    "roast_me": [
        L("You asked for it. Roast mode is on.", "R", cue="smug"),
        L("Oh, you want smoke? Roast mode.", "U", cue="hyped"),
        L("Roast mode, but classy. Judges are watching.", "J", cue="playful"),
        L("Roast mode? Okay. Gloves off.", "C", cue="playful"),
        L("Roast mode on.", "P"),
    ],
    "about": [
        L("I'm LOCKED IN. I babysit your phone so you actually work.", "R", cue="smug"),
        L("I'm the robot between you and your phone. Get to work.", "U", cue="deadpan"),
        L("I'm LOCKED IN! I hold your phone while you focus, and I back away if you reach for it.", "J", cue="energetic"),
        L("I'm LOCKED IN. I keep your phone company while you work.", "C", cue="warm"),
        L("I'm LOCKED IN: I hold your phone during focus sessions.", "P"),
    ],
}

LIBRARY.update(
    {
        "quiet_ack": [
            L("Going quiet. Talk to me if you need me.", "CJP"),
            L("Fine. Silent treatment. I'm still watching the phone.", "RU", cue="deadpan"),
        ],
        "goal_set": [L("Got it: {goal}.", ALL), L("Got it.", ALL)],
        "extended": [L("Okay, {added} more.", "CJP"), L("{added} more. Make them count.", "RU"), L("Fine. More time on the clock.", ALL)],
        "ack": [L("Heard you.", "CJP"), L("Noted.", "RU"), L("Okay.", ALL)],
        "led_unsupported": [L("I can't change my lights from here. The factory firmware doesn't allow it.", ALL)],
        "camera_back": [L("Camera's back. I can see the phone again.", ALL)],
        "cant_do": [L("I can't do that.", "CJP"), L("Nope. Not something I can do.", "RU")],
        "camera_off_ack": [L("Camera off. I'm not watching anything now.", ALL)],
        "camera_on_ack": [L("Camera coming back on.", ALL)],
        "mic_off_ack": [L("Microphone off. Turn it back on from the dashboard.", ALL)],
        # The model had words, but none it could say truthfully (numbers not on record, ...).
        "no_answer": [
            L("I was about to make something up. Ask me that again?", "RUJ"),
            L("I don't have a good answer to that one. Try me again?", "CP"),
        ],
    }
)

# The offline voice pack: short fixed lines to pre-generate in the cloud voice, so the demo
# runs on the robot's internet-less Wi-Fi in one consistent voice. (category, exact text).
# Each line still plays only when its mode and profanity setting allow it.
LIBRARY.update(
    {
        # AIRLOCK. The same plain words in every personality, and each one only said when it is true. "Unknown" never becomes "malicious".
        # The first of each is said first in a session (the clearest wording); repeats vary, so a second warning never sounds like a recording.
        # Urgent lines are short commands spoken slower and firmer (voice/delivery.py). Each states only what was observed: a device connected
        # "while" someone was there, never that they connected it; a face that does not match is never called anyone.
        "security_warning_usb": [
            L("Security alert. This workstation is armed and a new USB device was just connected. Step away.", ALL),
            L("Hands off. An unknown USB device just connected, and this workstation is armed.", ALL),
            L("Stop. A USB device this workstation doesn't know was just plugged in. Step away.", ALL),
            L("Security alert. New USB device detected. Back away from the computer, now.", ALL),
        ],
        "security_warning_app": [
            L("Security alert. This workstation is armed and a watched app just opened. Step away from the keyboard.", ALL),
            L("Stop. A watched app just opened on an armed workstation. Step away from the keyboard.", ALL),
            L("Hands off the keyboard. A watched app just opened while this workstation is armed.", ALL),
        ],
        "security_warning_incident": [
            L("Security alert. Step away now: the security system has opened an incident.", ALL),
            L("Step away from this computer. A security incident is now open.", ALL),
            L("Stop. The security system just opened an incident on this workstation. Move away from it.", ALL),
        ],
        "security_warning_correlated": [
            L("Security alert. Step away from the computer: the desk camera saw someone here as a new device connected.", ALL),
            L("Stop. The camera saw someone at this desk as a new device connected. Step away.", ALL),
            L("Hands off. A new device connected while the desk camera saw someone here.", ALL),
        ],
        # The desk camera's lines say what it saw. A face that matches is never "you" being verified, and a hand at the ports is never a device.
        "security_warning_presence": [
            L("Hey, someone's at your workstation while it's armed.", ALL),
            L("I see someone at the desk. Just so you know, this workstation is armed.", ALL),
            L("Hi there. This workstation is armed, so I'm paying attention.", ALL),
            L("Someone's at the desk, and this workstation is armed. I'm keeping an eye on it.", ALL),
        ],
        "security_warning_interaction": [
            L("Security alert. Someone is interacting with your computer while it's armed. Step away now.", ALL),
            L("Hands off. Someone is using this computer while it's armed.", ALL),
            L("Stop. This workstation is armed. Step away from the keyboard.", ALL),
        ],
        "security_warning_usb_unrecognized": [
            L("Security alert. A new USB device was connected while an unrecognized person was at the desk. Step away.", ALL),
            L("Stop. A new USB device connected, and the person at the desk wasn't recognized. Step away.", ALL),
            L("Hands off. Unknown USB device, and the face at this desk doesn't match the owner.", ALL),
        ],
        "security_warning_usb_person": [
            L("Security alert. A new USB device was connected while someone was at the desk. Step away.", ALL),
            L("Stop. A new USB device connected while someone was at this desk. Step away.", ALL),
            L("Hands off. A new USB device connected, and the camera saw someone at the desk.", ALL),
        ],
        "security_owner_seen": [
            L("Welcome back. I'm still armed until you disarm on the dashboard.", ALL),
            L("Good to see you. It's still armed until you disarm on the dashboard.", ALL),
            L("There you are. Still armed, though, until you disarm on the dashboard.", ALL),
        ],
        "security_camera_degraded": [
            L("I can't see the desk right now. I'm still watching USB devices and apps.", ALL),
            L("I've lost the desk camera. USB and app monitoring are still on.", ALL),
            L("The camera view is blocked or offline. I'm still watching USB devices and apps.", ALL),
        ],
        "security_armed_camera": [L("Armed. I'm watching the desk, USB devices and app launches.", ALL)],
        # Demo mode: what the presenter just did, in plain words. "Matches your enrollment" is a camera match, not proof of identity.
        "security_owner_seen_incident": [
            L("Welcome back. Something happened while you were away: ask me what happened. I'm still armed until you disarm.", ALL),
            L("Good to see you. There's an open incident from while you were gone. Ask me what happened. Still armed until you disarm.", ALL),
            L("There you are. Something happened while you were gone; ask me about it. I'm still armed until you disarm.", ALL),
        ],
        "security_demo_started": [L("Demo mode. Review the camera and monitoring readiness on the dashboard.", ALL)],
        "security_demo_enroll_start": [L("Look at the camera, then turn your head slightly to each side.", ALL)],
        "security_demo_enrolled": [L("Got it. You're enrolled for this demo, and only on this Mac.", ALL)],
        "security_demo_recognized": [L("That's you. Your face matches this demo's enrollment.", ALL)],
        "security_demo_ended": [L("Demo ended. The demo session has been cleared.", ALL)],
        "security_arm_on_dashboard": [L("Arming happens on the security dashboard. Check it for the current session.", ALL)],
        "quiet_mode_on": [L("Okay. I'll stay quiet unless something important happens.", ALL), L("Sure. I'll only speak up for something important.", ALL)],
        "security_arm_needs_grant": [L("Approve voice arming on the dashboard for this request, then tell me again.", ALL),
                                     L("I need your go-ahead on the dashboard for a new arming request, then ask me again.", ALL)],
        "security_arm_refused": [L("The security database refused this arming request. Check the dashboard for the current session.", ALL)],
        "security_arm_unreachable": [L("I can't reach the security database to confirm this arming request. Check the dashboard for the current session.", ALL)],
        "security_arm_unconfirmed": [L("I asked the database to arm, but it hasn't confirmed. Don't count on me until the dashboard says armed.", ALL)],
        "security_disarm_on_dashboard": [L("Disarm in the authenticated owner console. I have not disarmed the workstation.", ALL)],
        "security_armed": [L("Armed. I'm watching for new USB devices and app launches.", ALL)],
        "security_armed_degraded": [L("Armed, but the {problem} monitor has a problem. Check the dashboard.", ALL)],
        "security_already_armed": [L("I'm already armed.", ALL)],
        "security_not_armed": [L("I'm not armed.", ALL)],
        "security_confirm_arm": [L("Confirm on the dashboard to arm. I'm not armed yet.", ALL)],
        "security_disarm_needs_owner": [L("Disarming needs the owner's code on the dashboard. I'm still armed.", ALL)],
        "security_disarmed": [L("Disarmed. I've stopped watching for this session.", ALL)],
        "security_expired": [L("My armed session has ended. I've stopped watching.", ALL)],
        "security_lock_requested": [L("I am requesting a screen lock.", ALL)],
        "security_lock_confirmed": [L("The screen is locked.", ALL)],
        "security_lock_failed": [L("I could not lock the screen.", ALL)],
        "security_lock_dry_run": [L("Dry run. I did not lock the screen.", ALL)],
        "security_checking": [L("Let me check the evidence.", ALL)],
        "security_report": [L("{report}", ALL)],
        "mission_planning": [L("Reading your tests.", ALL), L("Okay. Let me see how bad it is.", "RU")],
        "mission_looking": [L("Let me look.", ALL), L("One sec.", ALL)],
        "mission_looking_near": [L("There's no {said}. Opening {file}.", ALL)],
        "mission_read_no_answer": [L("I read {what}, but I don't have an answer I'd trust yet. Ask me again.", ALL)],
        "mission_model_slow": [
            L("My head's running slow. Your mission is safe. Ask me again in a moment.", "CP"),
            L("Brain's lagging. Your mission's safe and the boss isn't going anywhere. Ask me again.", "RUJ"),
        ],
        "mission_tool_failed": [L("That didn't work. It's on the dashboard.", ALL)],
        "mission_tool_refused": [L("{reason}.", ALL)],
        "mission_start": [
            L("{mission}. The boss has {hp} health. Start with {checkpoint}.", ALL),
            L("{mission}. {hp} failing tests between you and done. {checkpoint} first.", "RUJ"),
        ],
        "mission_no_boss": [L("Nothing is failing, so there's no boss yet. Write a test that fails and I'll start counting.", ALL)],
        "mission_already": [L("We're already on {mission}. Finish it or drop it first.", ALL)],
        "mission_none": [L("No mission yet. Tell me what you're building.", ALL)],
        "mission_tests_wont_run": [L("I can't get your tests to run, so there's no mission yet. It's on the dashboard.", ALL)],
        "mission_tests_started": [L("Running them.", ALL)],
        "mission_status": [L("{hp} of {hp_max} health left. Next, {checkpoint}.", ALL), L("The boss has {hp} health left. {checkpoint} is next.", "CRUJ")],
        "mission_hint": [L("On {checkpoint}, the test says: {summary}.", ALL)],
        "mission_stuck": [L("{checkpoint} has held you up {times} times. The test says: {summary}.", ALL)],
        "mission_ended": [L("Mission dropped. The boss lives.", ALL), L("Fine. The boss wins this one.", "RU")],
        "mission_edit_ready": [L("I put a suggested change on your screen. Nothing changes unless you approve it.", ALL)],
        "mission_edit_failed": [L("I don't have a change I'd trust for that one yet.", ALL)],
        "boss_hit": [
            L("Hit. {hp} health left.", ALL),
            L("That landed. {hp} to go.", "CRUJ"),
            L("{damage} down, {hp} left. Next, {checkpoint}.", "CRUJ"),
        ],
        "checkpoint_done": [
            L("{done} cleared. {hp} health left. Next, {checkpoint}.", ALL),
            L("The tests agree: {done} is done. On to {checkpoint}.", "CRUJ"),
        ],
        "boss_beaten": [
            L("Boss down. All {tests} tests pass. {xp} XP.", ALL),
            L("Boss is dead. All {tests} tests pass, and that's proof, not vibes. {xp} XP.", "RUJ"),
        ],
        "boss_healed": [L("A test that was passing now fails. The boss is back up to {hp}.", ALL), L("You just healed the boss. It's back to {hp}.", "RU")],
        "mission_regression": [L("{done} just broke again. The boss is back up to {hp}.", ALL)],
        "tests_edited": [L("You changed a test file, so that run doesn't count. Accept it on the dashboard if you meant it.", ALL)],
        "code_broken": [L("That doesn't load, so the tests can't even start. Check the last thing you changed.", ALL)],
        "tests_timeout": [L("The tests ran too long, so I stopped them. That run doesn't count.", ALL)],
        "reach_mission": [
            L("Hands off. {checkpoint} still needs you.", ALL),
            L("The phone won't fix {checkpoint}. Hands off.", "CRUJ"),
            L("The boss has {hp} health and you're reaching for the phone.", "RUJ"),
        ],
    }
)

VOICE_PACK: list[tuple[str, str]] = [
    ("session_start", "Alright, lock the fuck in."),
    ("session_start", "Okay. Phone's mine now. Go."),
    ("session_start", "Phone secured. Focus mode on. I'm watching."),
    ("reach_early", "Bro, we literally just started."),
    ("reach_early", "Whoa, already? We just started!"),
    ("reach_first", "Get your ass back to work."),
    ("reach_first", "Nice try."),
    ("reach_first", "Nice try, dumbass."),
    ("reach_first", "Nope! Not today!"),
    ("reach_repeat", "I saw that. Again."),
    ("reach_repeat", "Dumbass, that's your third attempt."),
    ("reach_repeat", "Again?! Bold strategy."),
    ("reach_repeat_many", "The phone is not the assignment."),
    ("nudge_distracted", "Eyes back on the work?"),
    ("nudge_distracted", "Psst. The work misses you."),
    ("progress_ack", "Okay, that was actually clean."),
    ("progress_ack", "Holy shit, you're actually cooking."),
    ("progress_ack", "Look at that. Real progress."),
    ("target_reached", "That's what I'm talking about!"),
    ("target_reached", "HOLY SHIT, YOU ACTUALLY DID IT."),
    ("pause", "Paused. Don't get comfortable."),
    ("pause", "Paused! Take a breather."),
    ("welcome_back", "Oh, look who's back."),
    ("welcome_back", "There you are!"),
    ("release", "Fine. Robot privileges revoked."),
    ("release", "Fine. Take your precious phone."),
    ("release", "Phone released! Thanks for focusing."),
    ("retreat_blocked", "Wheels are locked. I'm judging you from right here."),
    ("robot_back", "I'm back. Did you touch the phone? Don't answer that."),
    ("stop_ack", "Stopped."),
    ("estop_ack", "Emergency stop. I'm not moving."),
    # Coverage top-up: every main mode has a clean cached line for every category.
    ("welcome_back", "Oh, you're back. Good."),
    ("reach_early", "Bro. Already?"),
    ("reach_first", "I saw that. Everyone saw that."),
    ("session_start", "Phone's mine. Don't make me come over there."),
    ("nudge_distracted", "Hello? Your work called. It misses you."),
    ("pause", "Paused. The timer misses you already."),
    ("target_reached", "You did it. I'm genuinely shocked. Proud, but shocked."),
    ("reach_repeat_many", "Maybe it's time for a quick break?"),
]
# Optional extra for Chill mode.
VOICE_PACK_CHILL: list[tuple[str, str]] = [
    ("session_start", "Okay. Ease in, I'll hold the phone."),
    ("reach_early", "Already? We just started."),
    ("reach_repeat", "Again? Phone's fine, I promise."),
    ("nudge_distracted", "Hey. Eyes back on the work."),
    ("progress_ack", "Damn, nice. Keep going."),
    ("pause", "Paused. Take your time."),
    ("welcome_back", "Welcome back."),
    ("resume", "Back at it."),
    ("release", "All yours."),
]


def voice_pack(include_chill: bool = False) -> list[tuple[str, str, str | None]]:
    """(category, text, cue) for the pack. Every entry must be a fixed line of the library
    (or an expansion of its small ordinal slot), so what is cached is exactly what is said."""
    out = []
    for category, text in VOICE_PACK + (VOICE_PACK_CHILL if include_chill else []):
        line = next(
            (ln for ln in LIBRARY[category] if ln.text == text or (ln.slots == ["attempt_ordinal"] and text in [ln.text.format(attempt_ordinal=o) for o in ORDINALS.values()])),
            None,
        )
        if line is None:
            raise ValueError(f"voice pack line not in the library: {category}: {text!r}")
        out.append((category, text, line.cue))
    return out


# Short add-ons after a grounded status answer. No numbers, no claims.
STATUS_TAGS: dict[str, list[Line]] = {
    "chill": [L("Nice and steady.", "C")],
    "roast": [L("Keep cooking.", "R"), L("Not bad.", "R")],
    "unhinged": [L("Don't screw it up.", "U"), L("Keep it moving.", "U")],
    "judge_demo": [L("Not bad at all.", "J"), L("The phone is still with me.", "J")],
    "plain": [],
}


@dataclass
class Style:
    mode: str = "roast"
    profanity: str = "off"
    tone: str = "normal"
    unhinged_opt_in: bool = False
    supportive: bool = False  # the user asked for help recently

    @property
    def effective_mode(self) -> str:
        if self.mode == "unhinged" and not self.unhinged_opt_in:
            return "roast"
        return self.mode if self.mode in MODES else "roast"

    @property
    def pool(self) -> str:
        """Which line pool to draw from."""
        if self.tone == "serious":
            return "plain"
        if self.tone == "gentle" or self.supportive:
            return "chill"
        return self.effective_mode

    @property
    def max_profanity(self) -> int:
        if self.tone != "normal":
            return 0
        level = min(PROFANITY_LEVEL.get(self.profanity, 0), MODE_PROFANITY_CAP[self.effective_mode])
        return min(level, 1) if self.supportive else level

    @property
    def insults_allowed(self) -> bool:
        return self.pool == "unhinged" and self.tone == "normal" and not self.supportive


@dataclass
class Rendered:
    text: str
    category: str
    intent: str
    priority: int
    pool: str
    profanity: int = 0
    insult: bool = False
    cue: str | None = None
    cacheable: bool = False
    template: str = ""


def _fillable(line: Line, slots: dict) -> bool:
    return all(slots.get(name) not in (None, "") for name in line.slots)


@dataclass
class PersonalityEngine:
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.monotonic
    _recent: deque = field(default_factory=lambda: deque(maxlen=8))
    _last_by_category: dict = field(default_factory=dict)
    _session_used: dict = field(default_factory=dict)  # category -> lines said this session
    _last_insult_at: float | None = None
    _since_insult: int = 99

    def candidates(self, category: str, style: Style, slots: dict | None = None) -> list[Line]:
        lines = LIBRARY.get(category, LIBRARY["unknown"])
        pool = style.pool
        out = [ln for ln in lines if pool in ln.modes and ln.prof <= style.max_profanity]
        if not self._insult_ok(style):
            out = [ln for ln in out if not ln.insult]
        if slots is not None:
            out = [ln for ln in out if _fillable(ln, slots)]
        return out

    def _insult_ok(self, style: Style) -> bool:
        if not style.insults_allowed:
            return False
        if self._since_insult < INSULT_MIN_LINES_BETWEEN:
            return False
        return self._last_insult_at is None or self.clock() - self._last_insult_at >= INSULT_GAP_S

    def new_session(self) -> None:
        """Forget which lines were used, so a new session starts with the full variety."""
        self._session_used.clear()

    def render(self, category: str, slots: dict, style: Style, prefer: Callable[[str, str | None], bool] | None = None) -> Rendered:
        """``prefer(text, cue)``: True for lines the current voice can play instantly (cached
        cloud clips). When any eligible line qualifies, only those are used, so the robot does
        not switch voices mid-demo."""
        intent = INTENTS.get(category, INTENTS["unknown"])
        usable = self.candidates(category, style, slots)
        if not usable:  # e.g. serious tone and a joke-only category: fall back to plain lines
            plain = Style(mode=style.mode, tone="serious")
            usable = [ln for ln in LIBRARY.get(category, []) if "plain" in ln.modes and _fillable(ln, slots)]
            style = plain if usable else style
        if not usable:
            return Rendered("Okay.", category, intent.name, intent.priority, style.pool, cacheable=True, template="Okay.")
        last = self._last_by_category.get(category)
        if intent.priority == P3:
            static = [ln for ln in usable if not ln.slots or set(ln.slots) <= {"attempt_ordinal"}]
            if static and len(static) < len(usable) and self.rng.random() < STATIC_JOKE_SHARE:
                usable = static
        if prefer is not None:
            ready = [ln for ln in usable if prefer(ln.text.format(**{k: v for k, v in slots.items() if v is not None}), ln.cue)]
            if ready:
                usable = ready
        said = self._session_used.get(category, set())
        unsaid = [ln for ln in usable if ln.text not in said] or usable
        fresh = [ln for ln in unsaid if ln.text not in self._recent] or [ln for ln in unsaid if ln.text != last] or unsaid
        # A security line's first wording in a session is its clearest; later ones vary. Everything else is chosen at random as before.
        line = usable[0] if category.startswith("security_") and not said else self.rng.choice(fresh)
        self._recent.append(line.text)
        self._last_by_category[category] = line.text
        self._session_used.setdefault(category, set()).add(line.text)
        if line.insult:
            self._last_insult_at = self.clock()
            self._since_insult = 0
        else:
            self._since_insult += 1
        text = line.text.format(**{k: v for k, v in slots.items() if v is not None})
        verdict = guard_check(text, max_profanity=max(style.max_profanity, line.prof), allow_insults=True)
        if not verdict.ok:  # a template should never fail this; be safe anyway
            plain = [ln for ln in LIBRARY.get(category, []) if "plain" in ln.modes and _fillable(ln, slots)]
            text = plain[0].text.format(**slots) if plain else "Okay."
            line = plain[0] if plain else Line("Okay.", frozenset({"plain"}))
        return Rendered(
            text=text,
            category=category,
            intent=intent.name,
            priority=intent.priority,
            pool=style.pool,
            profanity=line.prof,
            insult=line.insult,
            cue=line.cue,
            cacheable=not line.slots or set(line.slots) <= {"attempt_ordinal"},
            template=line.text,
        )

    def status_tag(self, style: Style) -> str | None:
        if style.tone != "normal":
            return None
        tags = [t for t in STATUS_TAGS.get(style.pool, []) if t.prof <= style.max_profanity]
        return self.rng.choice(tags).text if tags else None


def cacheable_lines(styles: list[Style]) -> list[tuple[str, str, str | None]]:
    """(category, text, cue) for every fixed line these styles can say, with the small
    "attempt" ordinals expanded. Used to pre-generate cloud audio for offline use."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str | None]] = []
    for style in styles:
        for category, lines in LIBRARY.items():
            for ln in lines:
                if style.pool not in ln.modes or ln.prof > style.max_profanity:
                    continue
                if ln.insult and not style.insults_allowed:
                    continue
                variants: list[str]
                if not ln.slots:
                    variants = [ln.text]
                elif set(ln.slots) == {"attempt_ordinal"}:
                    variants = [ln.text.format(attempt_ordinal=o) for o in ORDINALS.values()]
                else:
                    continue
                for text in variants:
                    if (category, text) not in seen:
                        seen.add((category, text))
                        out.append((category, text, ln.cue))
    return out
