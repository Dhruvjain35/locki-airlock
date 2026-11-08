"""Milestone 5: the companion layer between the runtime and the local language model.

It owns the bounded session context (context.py), the behaviour engine (behavior.py), the
dialogue engine (dialogue.py) and the log of typed action requests (actions.py). The
runtime calls it at a few points: a user turn the deterministic parser should not settle
alone, vision and session events, the tick. It never sends a motor command: physical
requests go to the runtime's safety controller like every other request.

The context is event-sourced from the runtime's bus: only things that were published as
events (a confirmed reach, a camera drop, a line that actually played) become OBSERVED
facts. What the user says is recorded as what they said.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import deque

from ..missions.workspace import PolicyError, asks_to_see_file
from ..telemetry.events import EventSource, EventType
from ..voice.intents import AIRLOCK_INTENTS, MISSION_INTENTS, IntentName
from ..voice.tts import PRIORITY_AMBIENT, PRIORITY_REPLY
from .actions import MISSION_TOOLS, PHYSICAL, READ_TOOLS, ActionName, ActionRequest, goal_from_question
from .behavior import BehaviorDecision, BehaviorEngine
from .context import SessionContext
from .dialogue import (
    DialogueEngine,
    DialogueResult,
    allowed_actions,
    check_reply,
    numbers_in,
    session_line,
    style_line,
)
from .state_machine import State

log = logging.getLogger(__name__)

# Settled by the deterministic parser alone, however the sentence goes on: stopping is
# always safe, the phone is always the user's to take back, and turning the camera or the
# microphone off must never depend on a model's interpretation.
ALWAYS_REFLEX = frozenset(
    {IntentName.EMERGENCY_STOP, IntentName.STOP_MOTION, IntentName.RELEASE, IntentName.CAMERA_OFF, IntentName.CAMERA_ON, IntentName.MIC_OFF,
     # A fact the session record answers exactly and at once ("24 minutes left.").
     IntentName.TIME_LEFT, *MISSION_INTENTS, *AIRLOCK_INTENTS}
)
IMPERATIVE_STOP = re.compile(
    r"^\W*(please\s+)?(stop|freeze|halt)\b|\b(stop|freeze|halt)\s+(it|now|moving|that|the robot|right now|right there)\b|\b(stop|freeze|halt)\W*$",
    re.I,
)
# A recognised command this short is taken literally (fast, cached voice); a longer sentence
# that merely contains one goes to the model, with the parser's reading as a hint.
REFLEX_MAX_WORDS = 6
PREPARED_MAX_AGE_S = 150.0
PREPARE_EVERY_S = 20.0
QUIET_AFTER_USER_S = 10.0  # no background generation this soon after the user spoke
FALLBACK = {
    ActionName.START_SESSION: "session_start",
    ActionName.SET_GOAL: "goal_set",
    ActionName.EXTEND_SESSION: "extended",
    ActionName.PAUSE_SESSION: "pause",
    ActionName.RESUME_SESSION: "resume",
    ActionName.END_SESSION: "end_early",
    ActionName.SET_QUIET: "quiet_ack",
    ActionName.SET_STYLE: "ack",
    ActionName.RELEASE_PHONE: "release",
    ActionName.REQUEST_RETREAT: "retreat_blocked",
    ActionName.REQUEST_RETURN: "return_refused",
    ActionName.SET_LED: "led_unsupported",
    ActionName.STOP: "stop_ack",
    ActionName.RUN_TESTS: "mission_tests_started",
    ActionName.READ_FILE: "mission_hint",
    ActionName.READ_TEST_OUTPUT: "mission_hint",
    ActionName.PROPOSE_EDIT: "mission_edit_failed",
    ActionName.ABANDON_MISSION: "mission_ended",
}
# While a mission runs the model chooses from the mission's tools and these. Everything else stays with the
# parser and the dashboard: measured on Qwen3, a longer list only gave it more wrong things to pick.
KEPT_DURING_MISSION = frozenset({ActionName.PAUSE_SESSION, ActionName.RESUME_SESSION, ActionName.SET_QUIET, ActionName.RELEASE_PHONE, ActionName.STOP})
ABOUT_THE_PHONE = re.compile(r"\b(phone|call|calling|text|texting|mine|it back)\b", re.I)
# Their results are real and take time, so the model's words about them are not spoken: a fixed line is.
ACT_TOOLS = frozenset(MISSION_TOOLS) - READ_TOOLS
LED_REFUSAL = "the robot's factory firmware (1.4.5) has no LED control over the link (docs/legacy/HARDWARE_PROTOCOL.md)"


class Companion:
    def __init__(self, rt):
        self.rt = rt
        self.context = SessionContext()
        self.behavior = BehaviorEngine(self.context, user_talking=self._user_talking)
        self.behavior.on_decision = self._publish_decision
        self.llm = None
        self.dialogue: DialogueEngine | None = None
        self.requests: deque[dict] = deque(maxlen=30)
        self.replies: deque[dict] = deque(maxlen=20)
        self.prepared: dict | None = None
        self._prepare_task: asyncio.Task | None = None
        self._last_prepare = 0.0
        self._last_user_turn = 0.0
        self._last_speech_start = 0.0
        self._last_possible_obs = 0.0
        self.tool_output: str | None = None  # what the last read tool returned, for the follow-up
        # (monotonic, wall, facts) when the user started talking: the turn's context is rendered
        # as of then, so the prefilled text matches what the model reads after Whisper.
        self._anchor: tuple[float, float, object] | None = None
        rt.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        s = self.rt.settings
        if s.llm_provider != "local" or not self.rt.enable_llm:
            return
        if s.local_llm_backend == "ollama" and self._ollama_serves(s):
            from .ollama_llm import OllamaLLM

            self.llm = OllamaLLM(s.ollama_model, base=s.ollama_url)
        else:
            from .local_llm import LocalLLM

            self.llm = LocalLLM(s.local_llm_model, s.models_dir)
        self.dialogue = DialogueEngine(self.llm, timeout_s=s.local_llm_timeout_s)

    @staticmethod
    def _ollama_serves(s) -> bool:
        """Ollama was chosen: use it only if it answers at ollama_url with ollama_model pulled (the M5 tunnel may be down). Otherwise the
        conversation runs on this Mac's own model and says so in the log; security warnings never depended on either. A model name the adapter
        refuses (cloud-hosted, untagged) is never swapped for another: it stays on Ollama and shows as refused."""
        from .ollama_llm import _refusal, serves

        if _refusal(s.ollama_model) or serves(s.ollama_url, s.ollama_model):
            return True
        log.warning("Ollama at %s does not serve %s; the conversation uses the local model %s", s.ollama_url, s.ollama_model, s.local_llm_model)
        return False

    def close(self) -> None:
        if self._prepare_task is not None:
            self._prepare_task.cancel()
        if self.llm is not None:
            self.llm.close()

    @property
    def available(self) -> bool:
        return self.dialogue is not None and self.dialogue.available and self.rt.rs.conversation.generated_replies

    @property
    def proactive_available(self) -> bool:
        return self.available and self.rt.rs.conversation.proactive_generated

    def _user_talking(self) -> bool:
        v = self.rt.voice
        if v is None:
            return False
        return bool(getattr(v, "_ptt_active", False) or getattr(getattr(v, "vad", None), "in_speech", False))

    # ------------------------------------------------------------------ event-sourced context
    def _on_event(self, ev) -> None:
        et, p, ctx = ev.event_type, ev.payload, self.context
        sim = " (SIMULATED)" if ev.simulated else ""
        rt = self.rt
        if et == EventType.SESSION_STARTED:
            ctx.new_session(ev.session_id)
            self.behavior.reset_session()
            self.prepared = None
            ctx.observe("session_started", f"session started: {p.get('task')}, {round((p.get('planned_s') or 0) / 60)} minutes planned")
        elif et == EventType.SESSION_PAUSED:
            ctx.observe("paused", "session paused")
        elif et == EventType.SESSION_RESUMED:
            ctx.observe("resumed", "session resumed")
        elif et == EventType.SESSION_ENDED:
            ctx.observe("session_ended", f"session ended ({p.get('reason')})")
            self.prepared = None
        elif et == EventType.SESSION_PROGRESS:
            ctx.observe("progress", f"user reported progress: {p.get('total')} {p.get('unit_label')} done")
        elif et == EventType.VISION_PHONE_REACH_CONFIRMED and rt.sessions.active:
            ctx.observe("reach_confirmed", f"hand reached for the phone, confirmed by the camera{sim}")
        elif et == EventType.VISION_HAND_APPROACHING and rt.sessions.active and not rt.sessions.paused:
            now = time.monotonic()
            if now - self._last_possible_obs > 30.0:
                self._last_possible_obs = now
                ctx.observe("reach_possible", f"a hand moved toward the phone, NOT confirmed{sim}")
        elif et == EventType.VISION_CAMERA_DISCONNECTED:
            ctx.observe("camera_lost", "camera feed lost: not watching the phone")
        elif et == EventType.VISION_CAMERA_CONNECTED and ctx.last("camera_lost") is not None:
            ctx.observe("camera_back", "camera feed back")
        elif et == EventType.ROBOT_DISCONNECTED and p.get("reason") != "operator disconnect":
            ctx.observe("robot_lost", "robot link lost")
        elif et == EventType.ROBOT_CONNECTED:
            ctx.observe("robot_back", f"robot link connected{sim}")
        elif et == EventType.ROBOT_MOTION_BLOCKED:
            ctx.observe("motion_refused", f"a movement was refused by the safety system: {str(p.get('reason') or p.get('reason_code'))[:90]}")
        elif et == EventType.SAFETY_EMERGENCY_STOP:
            ctx.observe("estop", "emergency stop latched")
        elif et == EventType.MISSION_STARTED:
            ctx.observe("mission", f"mission started: {p.get('title')}; boss health {p.get('hp')} (one per failing test)")
        elif et == EventType.MISSION_TEST_RUN:
            counts = p.get("counts") or {}
            ctx.observe("test_run", f"test run ({p.get('status')}): {counts.get('passed', 0)} passed, {counts.get('failed', 0) + counts.get('error', 0)} failed; boss health {p.get('hp')}")
        elif et == EventType.MISSION_WON:
            ctx.observe("mission_won", f"every test passes: boss beaten, mission {p.get('title')} finished")
        elif et == EventType.MISSION_ABANDONED:
            ctx.observe("mission_dropped", f"the user dropped the mission {p.get('title')}")
        elif et == EventType.VOICE_SPEECH_STARTED:
            self.speculate()
        elif et == EventType.VOICE_TRANSCRIBED:
            text = p.get("text") or ""
            ctx.user_said(text, p.get("source") or "voice")
            self._last_user_turn = time.monotonic()
            if self.behavior.user_turn(text):
                ctx.observe("complaint", "the user objected to the robot's remarks")
        elif et == EventType.VOICE_TTS_FINISHED and p.get("source") not in ("none", None) and p.get("text"):
            outcome = p.get("outcome") or ""
            if outcome == "finished" or outcome.startswith("fallback") or outcome == "interrupted":
                cat = p.get("category") or ""
                kind = "generated" if cat.startswith("generated") else ("cached" if (p.get("source") or "").endswith("cache") else "template")
                trace = (p.get("trace") or {}).get("kind")
                text = p["text"] + (" (cut off)" if outcome == "interrupted" else "")
                ctx.robot_said(text, kind, proactive=trace not in ("turn", None) or cat.startswith("generated_proactive"))

    # ------------------------------------------------------------------ routing
    def route(self, intent, text: str, gate: str | None, source: str) -> str:
        """"reflex": the deterministic parser settles it. "converse": the model interprets it."""
        if not self.available and not (intent.name == IntentName.UNKNOWN and self._asks_to_see_a_named_file(text, source)):
            return "reflex"
        if intent.name == IntentName.SECURITY_DESK and gate not in ("conflict", "wake_word"):
            return "converse"  # the model answers from the SECURITY lines it is given; the fixed answer is only for when it is down
        if intent.name in ALWAYS_REFLEX - {IntentName.STOP_MOTION} or gate in ("motion_confidence", "conflict", "wake_word"):
            return "reflex"
        if intent.stop_word and (intent.name == IntentName.STOP_MOTION or intent.name in ALWAYS_REFLEX):
            # The motion stop has already happened (fail-safe, before routing). "Stop" as a
            # command ends the turn here; "I can't stop checking my phone" is conversation.
            if len((text or "").split()) <= 5 or IMPERATIVE_STOP.search(text or ""):
                return "reflex"
            if intent.name in ALWAYS_REFLEX - {IntentName.STOP_MOTION}:
                return "reflex"
        hands_free = source == "voice" and self.rt.rs.voice.listen_mode != "push_to_talk"
        if gate in ("unknown", "low_confidence"):
            # Hands-free, unrecognised speech is probably the room talking: not answered (M4).
            return "reflex" if hands_free else "converse"
        if len((text or "").split()) <= REFLEX_MAX_WORDS:
            return "reflex"
        return "converse"

    def _asks_to_see_a_named_file(self, text: str, source: str) -> bool:
        """Needs no model to read it or to refuse it, so it still works while the model is down."""
        hands_free = source == "voice" and self.rt.rs.voice.listen_mode != "push_to_talk"
        return not hands_free and asks_to_see_file(text) and self.rt.missions.file_named(text) is not None

    @property
    def model_down(self) -> bool:
        return self.llm is not None and self.dialogue is not None and not self.dialogue.available

    def _allowed(self) -> list[ActionName]:
        rt = self.rt
        a = allowed_actions(
            session_active=rt.sessions.active, paused=rt.sessions.paused, quiet=self.context.quiet(), holding_phone=rt.sm.state == State.HOLDING
        )
        if rt.missions.active:
            a = [x for x in a if x in KEPT_DURING_MISSION] + list(MISSION_TOOLS)
        return a

    def _camera_line(self, f) -> str:
        rt = self.rt
        if rt._sim_perception_used and not rt.vision_live:
            cam = "SIMULATED camera events (demo mode): reaches are simulated, say so if asked."
        elif rt.vision_live and not rt.zone_known:
            cam = "on, but the robot's marker is lost: not counting reaches right now."
        elif rt.vision_live:
            cam = "live, watching the phone."
        else:
            cam = "OFF. You cannot see the user or the phone right now."
        t = rt.transport
        if t is None or not t.connected:
            robot = "not connected."
        elif t.simulated:
            robot = "simulated robot (no hardware)."
        else:
            robot = "connected; wheels locked until the motor safety checks are done."
        return f"{cam} ROBOT: {robot}"

    def _watching(self) -> bool:
        """Something is actually looking: the live camera, or labelled simulated perception."""
        rt = self.rt
        return bool(rt.vision_live or rt._sim_perception_used)

    def speculate(self) -> None:
        """The user started talking (push-to-talk pressed, or speech detected): the model reads
        the context now, so after Whisper only the sentence itself is left to read."""
        self._last_speech_start = time.monotonic()
        if not self.available or self.llm is None or not hasattr(self.llm, "prefill"):
            return
        rt = self.rt
        f = rt.facts()
        self._anchor = (time.monotonic(), time.time(), f)
        prefix = self.context.render_prefix(
            style_line=style_line(rt.style()), session_line=session_line(f), camera_line=self._camera_line(f),
            allowed_actions=[a.value for a in self._allowed() if a != ActionName.STOP], anchor=self._anchor[:2],
        )
        self.llm.prefill(prefix)

    def render(self, *, allowed: list[ActionName], user_text: str | None = None, event: str | None = None, hint: str | None = None) -> str:
        rt = self.rt
        anchor = self._anchor if user_text is not None and self._anchor is not None and time.monotonic() - self._anchor[0] < 20.0 else None
        f = anchor[2] if anchor is not None else rt.facts()
        return self.context.render(
            style_line=style_line(rt.style()),
            session_line=session_line(f),
            camera_line=self._camera_line(f),
            allowed_actions=[a.value for a in allowed if a != ActionName.STOP],
            user_text=user_text,
            event=event,
            hint=hint,
            anchor=None if anchor is None else anchor[:2],
            mission=((rt.missions.context_block() or "") + _security_block(rt)) or None,
        )

    # ------------------------------------------------------------------ a user turn
    async def converse(self, text: str, source: str, intent, turn: dict | None, gate: str | None = None):
        rt = self.rt
        trace = None if turn is None else {"kind": "turn", "id": turn["id"], "t0": turn["heard_mono"]}
        hint = None
        if intent is not None and intent.name != IntentName.UNKNOWN:
            slots = {k: v for k, v in (intent.slots or {}).items() if v is not None}
            hint = f"a keyword parser read this as {intent.name.value}{' ' + str(slots) if slots else ''}. Decide yourself from the whole sentence."
        allowed = self._allowed()
        context = self.render(allowed=allowed, user_text=text, hint=hint)
        self._anchor = None  # one turn per press of the button
        spoken: list[str] = []
        guard = _security_guard(rt)  # None unless the SECURITY block is in this prompt
        refused: list[str] = []

        def on_sentence(sentence: str) -> None:
            if refused:
                return  # nothing after a refused sentence: it may lean on what was not said
            why = guard(sentence) if guard is not None else None
            if why is not None:
                refused.append(why)
                res_problems.append(f"not spoken: {why}")
                safe = rt.security.describe_record()  # the record itself, no model
                rt.speaker.say(safe, priority=PRIORITY_REPLY, category="security_record", trace=trace)
                spoken.append(safe)
                return
            rt.speaker.say(sentence, priority=PRIORITY_REPLY, category="generated", trace=trace)
            spoken.append(sentence)

        res_problems: list[str] = []

        # Which file gets read follows from what the person said. Asked for ../../secrets.py, Qwen3 sent cart.py
        # instead, so the refusal never fired and it answered from the wrong file.
        named = rt.missions.file_named(text)
        read_as_asked = named is not None and asks_to_see_file(text)
        if read_as_asked:
            res = DialogueResult(action=ActionRequest(ActionName.READ_FILE, {"path": named.path}, origin="voice_rule"), requested=ActionName.READ_FILE.value)
        else:
            res = await self.dialogue.respond(
                context,
                allowed=allowed,
                facts=rt.facts(),
                style=rt.style(),
                user_text=text,
                reach_recorded=self.context.count("reach_confirmed") > 0,
                recent_lines=self.context.recent_robot_lines(),
                on_sentence=on_sentence,
                camera_on=self._watching(),
                extra_numbers=rt.missions.numbers(),
                progress_claim_ok=rt.missions.progress_claim_ok(),
            )
            if named is not None and res.action is not None and res.action.name == ActionName.READ_FILE and res.action.args.get("path") != named.path:
                res.problems.append(f"model chose {str(res.action.args.get('path'))[:60]}; the person named {named.path[:60]}")
                res.action.args["path"] = named.path
        gen = res.generation
        model_failed = not read_as_asked and (gen is None or not gen.ok)
        if model_failed and not spoken and intent is not None and intent.name != IntentName.UNKNOWN and gate is None:
            # The model is unavailable or too slow: the parser's reading stands (Milestone 4),
            # but only if it passed the parser's own confidence gates.
            self._log_reply(text, res, kind="fallback_parser", spoken=None)
            r = await rt._dispatch_intent(intent, source)
            r.data["dialogue"] = res.to_payload()
            return r
        if model_failed and not spoken and gate == "low_confidence":
            self._log_reply(text, res, kind="fallback", spoken=None)
            return rt._result(False, "low confidence and no model reply", rt._say("low_confidence"), dialogue=res.to_payload())
        outcome = None
        if res.action is not None:
            if res.action.name == ActionName.READ_FILE and named is not None and named.corrected:
                rt._say("mission_looking_near", said=named.said, file=named.path)
            elif res.action.name in READ_TOOLS or res.action.name == ActionName.PROPOSE_EDIT:
                rt._say("mission_looking")  # the real answer takes one more pass: say so now, guess nothing
            elif res.action.name == ActionName.RUN_TESTS:
                rt._say("mission_tests_started")
            outcome = await self.run_action(res.action, source=source, user_text=text)
            if res.action.name in READ_TOOLS and outcome["status"] != "executed":
                # A model asked to describe a failure invented the file's contents ("the secrets file is empty"),
                # so the reason itself is what gets said.
                res.say = None
                self.tool_output = None
                self._log_reply(text, res, kind="tool", spoken=None, outcome=outcome)
                reply = rt._say("mission_tool_refused", reason=(outcome.get("reason") or "I can't read that").rstrip("."))
                return rt._result(False, f"{outcome['name']}: {outcome['status']} ({outcome.get('reason')})", reply, dialogue=res.to_payload(), action_request=outcome)
            if res.action.name in READ_TOOLS:
                res = await self._answer_from_tool(res, outcome, text, on_sentence, misheard=named.said if named is not None and named.corrected else None)
                if not res.say and not spoken:
                    # Read, but no answer: it timed out, or a check refused the words. Say that, not "Okay."
                    self._log_reply(text, res, kind="fallback", spoken=None, outcome=outcome)
                    what = (outcome.get("args") or {}).get("path") or "the test output"
                    return rt._result(True, f"{outcome['name']}: executed, no answer", rt._say("mission_read_no_answer", what=what), dialogue=res.to_payload(), action_request=outcome)
            elif res.action.name in ACT_TOOLS:
                # The tool has already said what really happened.
                res.say = None
                spoken_now = rt.last_reply.get("text") if rt.last_reply else None
                self._log_reply(text, res, kind="tool", spoken=spoken_now, outcome=outcome)
                ok = outcome["status"] in ("executed", "ignored")
                reply = spoken_now if ok else rt._say("mission_tool_failed")
                return rt._result(ok, f"{outcome['name']}: {outcome['status']}" + (f" ({outcome['reason']})" if outcome.get("reason") else ""), reply, dialogue=res.to_payload(), action_request=outcome)
        res.problems.extend(res_problems)
        reply = res.say
        kind = "generated"
        if reply and not spoken and guard is not None:
            for sentence in re.split(r"(?<=[.!?])\s+", reply):
                why = guard(sentence)
                if why is not None:
                    res.problems.append(f"not spoken: {why}")
                    res.rejected_say, reply = reply, rt.security.describe_record()
                    break
        if outcome is not None and outcome["status"] in ("refused", "failed") and not spoken and not (res.action is not None and res.action.name in READ_TOOLS):
            reply = None  # the words assumed the action happened; say what really happened
        if reply and not spoken:
            rt.speaker.say(reply, priority=PRIORITY_REPLY, category="generated", trace=trace)
        if not reply and not spoken and res.action is None and outcome is None and gate is None and intent is not None and intent.name != IntentName.UNKNOWN:
            # The model's words were refused: the parser's reading of the sentence stands.
            self._log_reply(text, res, kind="fallback_parser", spoken=None)
            r = await rt._dispatch_intent(intent, source)
            r.data["dialogue"] = res.to_payload()
            return r
        if not reply and not spoken:
            kind = "fallback"
            if res.action is None and res.requested not in (None, "none") and not model_failed:
                category = "cant_do"  # the model asked for something outside its list
            elif res.action is None and not model_failed:
                category = "no_answer"  # it had words, none of them true enough to say
            elif res.action is None and model_failed:
                # It heard fine: "say that again" would send the person after the wrong problem.
                category = "mission_model_slow" if rt.missions.active else "unknown"
            else:
                category = FALLBACK.get(res.action.name if res.action else None, "ack")
            slots = {}
            if res.action is not None:
                slots = {"goal": res.action.args.get("goal"), "added": f"{res.action.args.get('minutes')} minutes" if res.action.args.get("minutes") else None}
                if outcome is not None and outcome["status"] != "executed":
                    category = {"request_retreat": "retreat_blocked", "request_return": "return_refused", "set_led": "led_unsupported"}.get(
                        res.action.name.value, "ack"
                    )
                    slots["reason"] = (outcome.get("reason") or "not allowed")[:80]
            reply = rt._say(category, **{k: v for k, v in slots.items() if v})
        elif spoken:
            reply = " ".join(spoken)
        if turn is not None and kind == "generated":
            turn["replies"].append({"category": "generated", "text": reply, "kind": "generated"})
            turn["generated"] = True
        if kind == "generated":
            rt.last_reply = {"text": reply, "category": "generated", "at": time.time(), "audible": not rt.speaker.muted, "priority": f"P{PRIORITY_REPLY}"}
            rt.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"category": "generated", "text": reply, "source": "local language model", "audible": not rt.speaker.muted})
            if res.action is not None:
                rt.expression.for_category(FALLBACK.get(res.action.name, "ack"), simulated=bool(rt.transport and rt.transport.simulated))
        self._log_reply(text, res, kind=kind, spoken=reply, outcome=outcome)
        ok = outcome is None or outcome["status"] in ("executed", "ignored")
        msg = "conversation" if outcome is None else f"{outcome['name']}: {outcome['status']}" + (f" ({outcome['reason']})" if outcome.get("reason") else "")
        return rt._result(ok, msg, reply, dialogue=res.to_payload(), action_request=outcome)

    async def _answer_from_tool(self, first, outcome: dict, text: str, on_sentence, misheard: str | None = None):
        """A read tool ran: the model answers once more from the real result."""
        rt = self.rt
        result, self.tool_output = self.tool_output or "", None
        if not self.available:
            return DialogueResult(action=first.action, requested=first.requested, problems=[*first.problems, "no model to answer from the result"])
        what = f"{outcome['name']} of {outcome['args']['path']}" if (outcome.get("args") or {}).get("path") else outcome["name"]
        correction = f" There is no {misheard}: they meant this file, so use this file's name." if misheard else ""
        block = f"\nTOOL RESULT from {what}:\n{result}\nAnswer the user now from this, in your own short words. If they asked about something else, say what you read instead.{correction} Action none."
        second = await self.dialogue.respond(
            self.render(allowed=[], user_text=text) + block, allowed=[], facts=rt.facts(), style=rt.style(), user_text=text,
            reach_recorded=self.context.count("reach_confirmed") > 0, recent_lines=self.context.recent_robot_lines(), on_sentence=on_sentence,
            extra_numbers=rt.missions.numbers() | numbers_in(result), camera_on=self._watching(), progress_claim_ok=rt.missions.progress_claim_ok(),
        )
        second.action, second.requested = first.action, first.requested
        second.problems = first.problems + second.problems
        return second

    def _log_reply(self, user_text: str, res, *, kind: str, spoken: str | None, outcome: dict | None = None, event: str | None = None) -> None:
        entry = {
            **res.to_payload(),  # first: its "action" is only the request; the resolved one replaces it
            "id": uuid.uuid4().hex[:8],
            "at": time.time(),
            "user": user_text,
            "event": event,
            "kind": kind,  # generated | prepared | fallback | fallback_parser
            "spoken": spoken,
            "action": None if outcome is None else {k: outcome.get(k) for k in ("name", "args", "status", "reason")},
        }
        self.replies.append(entry)
        self.rt.bus.publish(EventType.DIALOGUE_REPLY, EventSource.VOICE, entry)

    # ------------------------------------------------------------------ typed actions
    async def run_action(self, req: ActionRequest, source: str = "voice", user_text: str = "") -> dict:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "at": time.time(),
            "origin": req.origin,
            "name": req.name.value,
            "args": req.args,
            "physical": req.name in PHYSICAL,
            "status": "requested",
            "reason": None,
        }
        self.requests.append(entry)
        self.rt.bus.publish(EventType.ACTION_REQUESTED, EventSource.POLICY, dict(entry))
        try:
            status, reason = await self._execute(req, source, user_text)
        except Exception as exc:
            log.exception("action %s failed", req.name)
            status, reason = "failed", str(exc)[:160]
        entry.update(status=status, reason=reason, resolved_at=time.time())
        self.rt.bus.publish(EventType.ACTION_RESOLVED, EventSource.POLICY, dict(entry))
        return entry

    async def _execute(self, req: ActionRequest, source: str, user_text: str = "") -> tuple[str, str | None]:
        rt, a, n = self.rt, dict(req.args), req.name
        origin = f"{req.origin} ({source})"
        if n in MISSION_TOOLS:
            return await self._execute_mission_tool(n, a, origin, user_text)
        if n == ActionName.RELEASE_PHONE and rt.missions.active and not ABOUT_THE_PHONE.search(user_text or ""):
            # Measured on Qwen3: "push my code to GitHub" came back as release_phone, which would end the session.
            return "ignored", "the user did not ask for the phone"
        if a.get("goal") and goal_from_question(a["goal"], user_text):
            if n == ActionName.SET_GOAL:
                # Dropped, not refused: the words usually just answer the question, so they stand.
                return "ignored", "a question is not a goal (the user asked about it, they did not say they are working on it)"
            a.pop("goal")  # start the session, but not with a question as its goal
        if n == ActionName.START_SESSION:
            r = await rt.start_session(task=a.get("goal"), duration_min=a.get("minutes"), origin=origin, speak=False)
            if not r.ok:
                return "refused", r.message
            if a.get("goal"):
                self.context.set_goal(a["goal"], "user_said")
            if source == "voice":
                await rt._ensure_monitoring()
            return "executed", r.message
        if n == ActionName.SET_GOAL:
            self.context.set_goal(a["goal"], "user_said")
            if rt.sessions.active:
                rt.sessions.set_task(a["goal"])
            return "executed", f"goal: {a['goal']}"
        if n == ActionName.EXTEND_SESSION:
            if not rt.sessions.active:
                return "refused", "no active session"
            rt.sessions.extend(a["minutes"] * 60)
            if rt.sessions.remaining_seconds() > 300:
                rt.milestones_done.discard("five_left")
            self.context.observe("extended", f"session extended by {a['minutes']} minutes (user asked)")
            return "executed", f"extended by {a['minutes']} min"
        if n == ActionName.PAUSE_SESSION:
            r = await rt.pause_session(origin=origin, speak=False)
            return ("executed" if r.ok else "refused"), r.message
        if n == ActionName.RESUME_SESSION:
            r = await rt.resume_session(origin=origin, speak=False)
            return ("executed" if r.ok else "refused"), r.message
        if n == ActionName.END_SESSION:
            r = await rt.end_session(reason="user", origin=origin, speak=False)
            return ("executed" if r.ok else "refused"), r.message
        if n == ActionName.SET_QUIET:
            self.context.set_quiet(a.get("minutes", 10))
            self.context.observe("quiet", f"quiet mode on for {a.get('minutes', 10)} minutes (user asked)")
            return "executed", f"quiet for {a.get('minutes', 10)} min"
        if n == ActionName.SET_STYLE:
            patch = {
                "chill": {"coach": {"personality": "chill", "tone": "normal"}},
                "roast": {"coach": {"personality": "roast", "tone": "normal"}},
                "serious": {"coach": {"tone": "serious"}},
            }[a["style"]]
            if all(getattr(rt.rs.coach, k) == v for k, v in patch["coach"].items()):
                # The model often names the style already in use (docs/NVIDIA_EVALUATION.md): nothing happened.
                return "ignored", f"style already {a['style']}: nothing to change"
            rt.update_settings(patch, publish=True)
            return "executed", f"style {a['style']}"
        if n == ActionName.RELEASE_PHONE:
            r = await rt.release(origin=origin, speak=False)
            return ("executed" if r.ok else "refused"), r.message
        if n == ActionName.STOP:
            r = await rt.stop_robot(origin=origin)
            return "executed", r.message
        if n == ActionName.SET_LED:
            return "refused", LED_REFUSAL
        if n == ActionName.REQUEST_RETURN:
            r = await rt.return_home(origin=origin)
            return ("executed" if r.ok else "refused"), r.message
        if n == ActionName.REQUEST_RETREAT:
            from ..robot.commands import MotionOrigin, MotionPrimitive, MotionProfileName, MotionRequest

            m = rt.rs.motion
            motion = MotionRequest(
                primitive=MotionPrimitive(m.retreat_primitive),
                speed=m.retreat_speed,
                duration_s=m.retreat_duration_s,
                profile=MotionProfileName.SHORT_RETREAT,
                origin=MotionOrigin.POLICY,
                reason="language model request (the user asked it to back off)",
            )
            # Only a confirmed reach puts the state machine where a retreat is allowed at all.
            sd = await rt.safety.request(motion, state_allows=rt.sm.state == State.PHONE_REACH_CONFIRMED, state_name=rt.sm.state.value)
            if sd.approved:
                return "executed", "safety controller approved a short retreat"
            return "refused", f"safety controller: {sd.reason}"
        return "refused", f"no handler for {n.value}"

    async def _execute_mission_tool(self, n: ActionName, a: dict, origin: str, user_text: str = "") -> tuple[str, str | None]:
        missions = self.rt.missions
        if not missions.active:
            return "refused", "no mission is running"
        try:
            if n == ActionName.RUN_TESTS:
                run = await missions.run_tests(f"requested by {origin}", speak_always=True)
                return ("executed", f"{run.status}: {run.counts()}") if run is not None else ("refused", "no mission is running")

            async def read_file():
                return missions.tool_read_file(a["path"])

            async def read_output():
                return missions.tool_test_output(user_text)

            if n == ActionName.READ_FILE:
                self.tool_output = await missions.relay_tool("read_file", {"path": a["path"]}, read_file, lambda t: {"chars": len(t)})
                return "executed", f"read {a['path']}"
            if n == ActionName.READ_TEST_OUTPUT:
                self.tool_output = await missions.relay_tool("read_test_output", {}, read_output, lambda t: {"chars": len(t)})
                return "executed", "read the last test output"
        except PolicyError as exc:
            return "refused", str(exc)
        if n == ActionName.PROPOSE_EDIT:
            r = await missions.propose_edit(origin=origin)
            return ("executed" if r.ok else "refused"), r.message
        r = await missions.abandon(origin=origin, speak=True)
        return ("executed" if r.ok else "refused"), r.message

    # ------------------------------------------------------------------ unprompted speech
    def _publish_decision(self, d: BehaviorDecision) -> None:
        self.rt.bus.publish(EventType.BEHAVIOR_DECISION, EventSource.POLICY, d.to_payload())

    def _fingerprint(self) -> tuple:
        """What a prepared line depends on (robot lines excluded: a heads-up right before the
        confirmation must not throw the prepared reaction away)."""
        c = self.context
        users = [t.mono for t in c.turns if t.role == "user"]
        rt = self.rt
        return (
            c.session_id,
            len(users),
            users[-1] if users else None,
            None if c.goal is None else c.goal.text,
            c.quiet(),
            c.complaints_within(600),
            rt.style().pool,
            rt.style().max_profanity,
        )

    def react(self, d: BehaviorDecision, *, say_as: str | None = None, trace: dict | None = None, beat: bool = False, priority: int | None = None,
              intervention: bool = False, slots: dict | None = None, fixed_as: str | None = None) -> str | None:
        """Speak an unprompted decision now: the prepared line when it fits, else a fixed line.
        (Low-urgency remarks may instead be written on the spot: see ``react_generated``.)"""
        if not d.speak:
            return None
        rt = self.rt
        text, source = None, None
        p = self.prepared
        if d.generate and say_as is None and self.proactive_available and p is not None and p["event"] == d.event:
            fresh = time.monotonic() - p["mono"] < PREPARED_MAX_AGE_S and p["fingerprint"] == self._fingerprint() and p["n"] == d.escalation
            if fresh:
                verdict = check_reply(
                    p["text"], facts=rt.facts(), style=rt.style(), reach_recorded=True, recent_lines=self.context.recent_robot_lines(),
                    extra_numbers=set(d.extra_numbers) | rt.missions.numbers(), progress_claim_ok=rt.missions.progress_claim_ok(),
                )
                if verdict.ok:
                    text, source = verdict.text, "prepared"
            self.prepared = None
        if text is not None:
            delay = rt._beat_rng.uniform(0.25, 0.6) if beat else 0.0
            audible = rt.speaker.say(text, priority=PRIORITY_AMBIENT if priority is None else priority, category="generated_proactive", trace=trace, delay_s=delay)
            payload = {"category": "generated_proactive", "text": text, "audible": audible, "source": "prepared by the local language model", "event": d.event}
            if intervention:
                rt.counters.interventions += 1
                rt.last_intervention_at = time.monotonic()
                rt.bus.publish(EventType.COACH_INTERVENTION_GENERATED, EventSource.POLICY, payload)
            else:
                rt.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, payload)
            rt.last_reply = {"text": text, "category": "generated_proactive", "at": time.time(), "audible": audible, "priority": "P3"}
            rt.expression.for_category(d.category or "ack", simulated=bool(rt.transport and rt.transport.simulated))
            self._log_reply("", _Prepared(p), kind="prepared", spoken=text, event=d.event)
        else:
            text = rt._say(say_as or fixed_as or d.category, intervention=intervention, beat=beat, trace=trace, priority=priority, **(slots or {}))
            source = "cached"
        d.said, d.source = text, source
        self._schedule_prepare(force=True)
        return text

    async def react_generated(self, d: BehaviorDecision, *, priority: int = PRIORITY_AMBIENT, timeout_s: float = 4.0) -> str | None:
        """Low-urgency remarks (a real focus streak, the camera back): written for the moment
        if the model answers in time, else the fixed line."""
        if not d.speak:
            return None
        if not (d.generate and self.proactive_available):
            return self.react(d, priority=priority)
        rt = self.rt
        context = self.render(allowed=[], event=d.event_text)
        res = await self.dialogue.respond(
            context, allowed=[], facts=rt.facts(), style=rt.style(), proactive=True, reach_recorded=self.context.count("reach_confirmed") > 0,
            recent_lines=self.context.recent_robot_lines(), extra_numbers=set(d.extra_numbers) | rt.missions.numbers(), timeout_s=timeout_s, camera_on=self._watching(),
            progress_claim_ok=rt.missions.progress_claim_ok(),
        )
        if not res.say or not d.speak:
            self._log_reply("", res, kind="fallback", spoken=None, event=d.event)
            return self.react(d, priority=priority)
        audible = rt.speaker.say(res.say, priority=priority, category="generated_proactive")
        rt.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"category": "generated_proactive", "text": res.say, "audible": audible, "event": d.event})
        rt.last_reply = {"text": res.say, "category": "generated_proactive", "at": time.time(), "audible": audible, "priority": f"P{priority}"}
        d.said, d.source = res.say, "generated"
        self._log_reply("", res, kind="generated", spoken=res.say, event=d.event)
        return res.say

    # ------------------------------------------------------------------ anticipation
    def _schedule_prepare(self, force: bool = False) -> None:
        """Prepare the reaction to the next confirmed reach while nothing else is happening, so
        it is context-specific and still plays as fast as a cached clip."""
        rt = self.rt
        if not (self.proactive_available and rt.rs.conversation.anticipate):
            return
        if not (rt.sessions.active and not rt.sessions.paused) or self.context.quiet():
            return
        if self._prepare_task is not None and not self._prepare_task.done():
            return
        now = time.monotonic()
        if now - self._last_user_turn < QUIET_AFTER_USER_S or (not force and now - self._last_prepare < PREPARE_EVERY_S):
            return
        if self._user_talking() or now - self._last_speech_start < QUIET_AFTER_USER_S:
            return  # the user is (or was just) talking: the GPU is theirs
        if self.llm is not None and getattr(self.llm, "_current", None) is not None:
            return
        fp = self._fingerprint()
        p = self.prepared
        if p is not None and p["fingerprint"] == fp and now - p["mono"] < PREPARED_MAX_AGE_S - 30:
            return
        self._last_prepare = now
        self._prepare_task = asyncio.create_task(self._prepare(fp), name="companion-prepare")

    async def _prepare(self, fp: tuple) -> None:
        rt = self.rt
        text_event, n = self.behavior.next_reach_text(rt.sessions.active_seconds())
        context = self.render(allowed=[], event=text_event)
        try:
            res = await self.dialogue.respond(
                context, allowed=[], facts=rt.facts(), style=rt.style(), proactive=True, background=True, reach_recorded=True,
                recent_lines=self.context.recent_robot_lines(), extra_numbers={n} | rt.missions.numbers(), progress_claim_ok=rt.missions.progress_claim_ok(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("preparing a reaction failed")
            return
        if not res.say or fp != self._fingerprint():
            return
        self.prepared = {"event": "confirmed_reach", "n": n, "text": res.say, "fingerprint": fp, "mono": time.monotonic(), "at": time.time(),
                         "ms": round(res.ms, 1), "generation": res.to_payload().get("llm")}
        kokoro = getattr(rt, "kokoro", None)
        if kokoro is not None and kokoro.available():
            await asyncio.to_thread(kokoro.ensure, [res.say])  # the voice is ready too

    def tick(self) -> None:
        rt = self.rt
        running = rt.sessions.active and not rt.sessions.paused
        for d in self.behavior.tick(running=running):
            if d.speak:
                rt._say(d.category, priority=PRIORITY_REPLY - 1)  # P1: the user should know
                d.said, d.source = rt.last_reply.get("text") if rt.last_reply else None, "cached"
        self._schedule_prepare()

    # ------------------------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        p = self.prepared
        return {
            "provider": self.rt.settings.llm_provider,
            "available": self.available,
            "llm": None if self.llm is None else self.llm.describe(),
            "conversation": self.rt.rs.conversation.model_dump(),
            "context": self.context.snapshot(),
            "behavior": self.behavior.snapshot(),
            "requests": list(self.requests)[::-1][:15],
            "replies": list(self.replies)[::-1][:12],
            "prepared": None if p is None else {"text": p["text"], "n": p["n"], "age_s": round(time.monotonic() - p["mono"], 1), "ms": p["ms"]},
            "allowed_actions": [a.value for a in self._allowed()],
        }


class _Prepared:
    """Adapter so a prepared line is logged like a dialogue result."""

    def __init__(self, p: dict | None):
        self.p = p or {}

    def to_payload(self) -> dict:
        return {"say": self.p.get("text"), "action": None, "requested": None, "problems": [], "rejected_say": None, "streamed": 0,
                "ms": self.p.get("ms"), "llm": self.p.get("generation"), "prepared_age_s": round(time.monotonic() - self.p.get("mono", time.monotonic()), 1)}


def _security_guard(rt):
    """With the SECURITY block in the prompt: a check for each sentence the model wrote, the investigator's own (security/tools.py), returning
    why it must not be spoken, or None. No verdict or accusation, no lock the record does not confirm, no notification nobody sent. A sentence
    that denies the claim ("nothing proves it was a hacker") passes."""
    if not _security_block(rt):
        return None
    from ..security.tools import _DENIES, _LOCK_CLAIM, _NOTIFY_CLAIM, _VERDICT

    security = rt.security

    def check(sentence: str) -> str | None:
        if _DENIES.search(sentence):
            return None
        word = _VERDICT.search(sentence)
        if word:
            return f"a verdict or an accusation ({word.group(0)})"
        lock = security.locks[-1]["status"] if security.locks else None
        if _LOCK_CLAIM.search(sentence) and lock != "LOCK_CONFIRMED":
            return f"a lock the record does not confirm (it says {lock or 'no lock'})"
        if _NOTIFY_CLAIM.search(sentence) and not security.review_requests:
            return "a notification nobody sent"
        return None

    return check


def _security_block(rt) -> str:
    """What the desk camera and the Mac observed, current at this turn (AIRLOCK). Empty when security is off."""
    security = getattr(rt, "security", None)
    try:
        return security.context_block() if security is not None and hasattr(security, "context_block") else ""
    except Exception:
        return ""
