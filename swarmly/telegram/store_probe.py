"""Phase 0 of the topic registry: check the Telegram facts a Telegram-backed store relies on.

The design (``docs/design/topic-registry.md``, approved 24 Oct 2026) keeps the swarm's shared
state on Telegram itself: every write is a machine line in Swarm pulse, the lowest message id
wins a compare-and-set, and the whole state is a JSON file on the newest pinned message, which
a bot that starts reads through ``getChat``. Nothing is built on that until these hold on the
live bots, every one of them:

1. **Visibility.** Every bot receives every other bot's machine lines in Swarm pulse.
2. **One order.** A line has the same message id for every reader, and one bot's lines get
   rising ids in the order it sent them.
3. **The pin.** A bot can post a JSON file, pin it, and every other bot reads that file back
   through ``getChat`` -- while the dashboard is pinned too -- and downloads it intact.
4. **Rate.** A small burst goes out without a flood wait.

``/storetest@bot`` makes that bot the lead of one run. It posts ``step=start`` in Swarm pulse;
every bot (the lead too) sends three numbered lines; the lead posts and pins a JSON file and
says which message it is; every bot reads the pin back and posts its findings; the lead unpins
the file, writes ``storetest-<run>.json`` next to the other state files and posts a summary.
All of it is machine traffic (``NOTE intent=PROBE``): no model turn anywhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from swarmly.telegram.wire import Envelope, MessageType, format_message, parse_message

if TYPE_CHECKING:  # pragma: no cover
    from swarmly.telegram.channel import AgentChannel, ChannelEvent

logger = logging.getLogger("swarmly.telegram.store_probe")

INTENT = "PROBE"
#: Lines each bot sends in its burst.
BURST = 3
#: The lead waits this long after ``start`` before it posts the file: long enough for every
#: bot's burst to go out through its paced sender.
PIN_AFTER_SECONDS = 20.0
#: How long a bot waits for the ``pin`` line before it reports without it.
PIN_WAIT_SECONDS = 60.0
#: How long the lead waits for the peers' results.
RESULTS_WAIT_SECONDS = 60.0
#: The test file: big enough to be a realistic state snapshot, small enough to be quick.
SNAPSHOT_BYTES = 64 * 1024


def _fields(body: str) -> Dict[str, str]:
    """``key=value`` tokens of a probe line's first line; the rest is the JSON payload."""
    first = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    out: Dict[str, str] = {}
    for token in first.split():
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value
    return out


def _payload(body: str) -> Dict[str, Any]:
    lines = (body or "").strip().splitlines()
    if len(lines) < 2:
        return {}
    try:
        data = json.loads("\n".join(lines[1:]))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _message_id(sent: Any) -> Optional[int]:
    if isinstance(sent, bool):
        return None
    if isinstance(sent, int):
        return sent
    value = getattr(sent, "message_id", None)
    return value if isinstance(value, int) else None


@dataclass
class Run:
    """One run, as this bot sees it."""

    run: str
    lead: str
    started: float
    #: Where the lead reports the verdict: the chat and topic the command came from.
    origin: Optional[Tuple[int, Optional[int]]] = None
    #: (sender alias, line number) -> message id, as received (or as sent, for our own lines).
    lines: Dict[Tuple[str, int], int] = field(default_factory=dict)
    #: The order lines arrived here, as message ids.
    arrival: List[int] = field(default_factory=list)
    send_seconds: float = 0.0
    send_errors: List[str] = field(default_factory=list)
    pin_id: Optional[int] = None
    pin_sha: str = ""
    pin_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: The lead only: alias -> that bot's result.
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    results_event: asyncio.Event = field(default_factory=asyncio.Event)


class StoreProbe:
    """One bot's part in ``/storetest`` runs."""

    def __init__(self, channel: "AgentChannel", *, clock: Any = time.time) -> None:
        self.channel = channel
        self.clock = clock
        self.runs: Dict[str, Run] = {}
        #: Bots the admin left out of the test (``/storetest@bot -Reasonix``): a bot running
        #: the same harness and model as another adds nothing, and an old release never answers.
        self.skipped: set = set()
        self._tasks: set = set()

    # -- small helpers -------------------------------------------------------------------
    @property
    def alias(self) -> str:
        return str(self.channel.config.identity.alias)

    def peers(self) -> List[str]:
        return [p for p in self.channel.selftest.peers()
                if p.strip().lower() != self.alias.strip().lower()]

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _place(self) -> Tuple[int, Optional[int]]:
        chat = int(self.channel.config.identity.home_channel_id or 0)
        return chat, await self.channel._ensure_pulse_topic()

    async def _post(self, run: str, body: str) -> Optional[int]:
        chat, topic = await self._place()
        line = format_message(Envelope(type=MessageType.NOTE, to="*", intent=INTENT, ref=run),
                              body)
        sent = await self.channel.send(line, chat_id=chat, thread_id=topic, silent=True,
                                       allow_when_frozen=True, return_message_id=True)
        return _message_id(sent)

    # -- starting a run (the lead) -------------------------------------------------------
    def start(self, origin: Optional[Tuple[int, Optional[int]]] = None,
              skip: Tuple[str, ...] = ()) -> str:
        """Begin a run with this bot as the lead; returns the run id at once."""
        self.skipped = {name.strip() for name in skip if name.strip()}
        run = f"{int(self.clock())}-{self.alias.lower()}"
        self.runs[run] = Run(run=run, lead=self.alias, started=self.clock(), origin=origin)
        self._spawn(self._lead(run))
        return run

    async def _lead(self, run: str) -> None:
        state = self.runs[run]
        try:
            await self._post(run, f"step=start lead={self.alias}")
            burst = asyncio.get_running_loop().create_task(self._burst(state))
            await asyncio.sleep(PIN_AFTER_SECONDS)
            await self._pin_snapshot(state)
            await burst
            mine = await self._read_pin(state)
            state.results[self.alias] = self._result(state, mine)
            try:
                await asyncio.wait_for(self._all_results(state), RESULTS_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            await self._unpin(state)
            report = self.verdict(state)
            self._write(run, report)
            await self._post(run, "step=done\n" + json.dumps(
                {"ok": report["ok"], "summary": report["summary"]}, separators=(",", ":")))
            logger.info("store test %s: %s", run, report["summary"])
            if state.origin is not None:
                await self.channel.send(f"Store test {run}: {report['summary']}",
                                        chat_id=state.origin[0], thread_id=state.origin[1],
                                        allow_when_frozen=True)
        except Exception:                              # noqa: BLE001 - a test must not crash
            logger.warning("store test %s failed", run, exc_info=True)
            await self._unpin(state)

    async def _all_results(self, state: Run) -> None:
        while not all(p in state.results for p in self.peers() if p not in self.skipped):
            state.results_event.clear()
            await state.results_event.wait()

    async def _pin_snapshot(self, state: Run) -> None:
        bot = self.channel.bot
        chat, topic = await self._place()
        filler = "x" * max(0, SNAPSHOT_BYTES - 200)
        data = json.dumps({"run": state.run, "lead": self.alias, "at": self.clock(),
                           "filler": filler}).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        from aiogram.types import BufferedInputFile
        caption = format_message(Envelope(type=MessageType.NOTE, to="*", intent=INTENT,
                                          ref=state.run), f"step=snapshot sha={sha[:16]}")
        sent = await bot.send_document(chat_id=chat, message_thread_id=topic,
                                       document=BufferedInputFile(data, f"probe-{state.run}.json"),
                                       caption=caption, disable_notification=True)
        state.pin_id, state.pin_sha = sent.message_id, sha[:16]
        await bot.pin_chat_message(chat_id=chat, message_id=sent.message_id,
                                   disable_notification=True)
        await self._post(state.run, f"step=pin mid={sent.message_id} sha={sha[:16]}")
        state.pin_event.set()

    async def _unpin(self, state: Run) -> None:
        if state.pin_id is None:
            return
        try:
            chat, _topic = await self._place()
            await self.channel.bot.unpin_chat_message(chat_id=chat, message_id=state.pin_id)
        except Exception:                              # noqa: BLE001 - the admin can unpin
            logger.warning("store test %s: the probe file could not be unpinned", state.run,
                           exc_info=True)

    # -- every bot's part ----------------------------------------------------------------
    async def _burst(self, state: Run) -> None:
        began = self.clock()
        for n in range(1, BURST + 1):
            try:
                mid = await self._post(state.run, f"step=line n={n} at={self.clock():.3f}")
            except Exception as exc:                   # noqa: BLE001 - recorded, reported
                state.send_errors.append(f"{type(exc).__name__}: {exc}"[:200])
                continue
            if mid is None:
                state.send_errors.append(f"line {n}: no message id")
            else:
                state.lines[(self.alias, n)] = mid
        state.send_seconds = round(self.clock() - began, 2)

    async def _join(self, state: Run) -> None:
        try:
            await self._burst(state)
            try:
                await asyncio.wait_for(state.pin_event.wait(), PIN_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            mine = await self._read_pin(state)
            await self._post(state.run, "step=result\n" + json.dumps(
                self._result(state, mine), separators=(",", ":")))
        except Exception:                              # noqa: BLE001
            logger.warning("store test %s: this bot's part failed", state.run, exc_info=True)

    async def _read_pin(self, state: Run) -> Dict[str, Any]:
        """Read the chat's pinned message back and download its file."""
        out: Dict[str, Any] = {"pin": "no pin line", "download": "skipped"}
        if state.pin_id is None:
            return out
        bot = self.channel.bot
        chat, _topic = await self._place()
        try:
            got = await bot.get_chat(chat)
        except Exception as exc:                       # noqa: BLE001
            out["pin"] = f"getChat failed: {exc}"[:200]
            return out
        pinned = getattr(got, "pinned_message", None)
        pid = getattr(pinned, "message_id", None)
        if pid != state.pin_id:
            out["pin"] = f"newest pin is {pid}, not {state.pin_id}"
            return out
        document = getattr(pinned, "document", None)
        if document is None:
            out["pin"] = "the pinned message has no file"
            return out
        out["pin"] = "ok"
        try:
            buffer = await bot.download(document.file_id, destination=io.BytesIO())
            data = buffer.getvalue() if hasattr(buffer, "getvalue") else bytes(buffer.read())
        except Exception as exc:                       # noqa: BLE001
            out["download"] = f"failed: {exc}"[:200]
            return out
        sha = hashlib.sha256(data).hexdigest()[:16]
        out["download"] = "ok" if sha == state.pin_sha else f"sha {sha} != {state.pin_sha}"
        out["bytes"] = len(data)
        return out

    def _result(self, state: Run, pin: Dict[str, Any]) -> Dict[str, Any]:
        saw: Dict[str, Dict[str, int]] = {}
        for (sender, n), mid in state.lines.items():
            saw.setdefault(sender, {})[str(n)] = mid
        return {"alias": self.alias, "saw": saw, "arrival": state.arrival[-30:],
                "send_seconds": state.send_seconds, "send_errors": state.send_errors[:5],
                **pin}

    # -- inbound -------------------------------------------------------------------------
    def observe(self, envelope: Envelope, event: "ChannelEvent") -> None:
        """A peer's probe line. Never raises."""
        try:
            self._observe(envelope, event)
        except Exception:                              # noqa: BLE001
            logger.warning("store test: a probe line could not be read", exc_info=True)

    def _observe(self, envelope: Envelope, event: "ChannelEvent") -> None:
        run = str(envelope.ref or "")
        if not run:
            return
        _env, body = parse_message(event.text or "")
        fields = _fields(body)
        step = fields.get("step", "")
        sender = self.channel._alias_for(event)
        state = self.runs.get(run)
        if step == "start":
            if state is None:
                state = self.runs[run] = Run(run=run, lead=fields.get("lead", sender),
                                             started=self.clock())
                self._spawn(self._join(state))
            return
        if state is None:
            return
        if event.message_id is not None:
            state.arrival.append(int(event.message_id))
        if step == "line":
            try:
                n = int(fields.get("n", ""))
            except ValueError:
                return
            if event.message_id is not None:
                state.lines[(sender, n)] = int(event.message_id)
        elif step == "pin":
            try:
                state.pin_id = int(fields.get("mid", ""))
            except ValueError:
                return
            state.pin_sha = fields.get("sha", "")
            state.pin_event.set()
        elif step == "result" and state.lead == self.alias:
            data = _payload(body)
            if data:
                state.results[str(data.get("alias") or sender)] = data
                state.results_event.set()

    # -- the verdict (the lead) ----------------------------------------------------------
    def verdict(self, state: Run) -> Dict[str, Any]:
        everyone = [self.alias, *[p for p in self.peers() if p not in self.skipped]]
        # A skipped bot that answered anyway is not checked: it was left out on purpose.
        reported = [a for a in everyone if a in state.results]
        missing = [a for a in everyone if a not in state.results]
        problems: List[str] = []
        # 1. visibility: every reporter saw every other reporter's lines.
        for reader in reported:
            saw = state.results[reader].get("saw") or {}
            for sender in reported:
                if sender == reader:
                    continue
                got = len(saw.get(sender) or {})
                if got < BURST:
                    problems.append(f"{reader} saw {got}/{BURST} of {sender}'s lines")
        # 2. one order: the same line has the same id everywhere; a sender's ids rise.
        ids: Dict[Tuple[str, str], set] = {}
        for reader in reported:
            for sender, numbered in (state.results[reader].get("saw") or {}).items():
                for n, mid in (numbered or {}).items():
                    ids.setdefault((sender, n), set()).add(mid)
        for (sender, n), seen in sorted(ids.items()):
            if len(seen) > 1:
                problems.append(f"{sender} line {n} has different ids: {sorted(seen)}")
        for sender in reported:
            ordered = [min(ids[(sender, str(n))]) for n in range(1, BURST + 1)
                       if (sender, str(n)) in ids]
            if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
                problems.append(f"{sender}'s ids do not rise in send order: {ordered}")
        # 3. the pin and the file.
        for reader in reported:
            result = state.results[reader]
            if result.get("pin") != "ok":
                problems.append(f"{reader}: pin {result.get('pin')}")
            elif result.get("download") != "ok":
                problems.append(f"{reader}: download {result.get('download')}")
        # 4. rate.
        for reader in reported:
            for error in state.results[reader].get("send_errors") or []:
                problems.append(f"{reader}: send error {error}")
        slowest = max((float(state.results[r].get("send_seconds") or 0) for r in reported),
                      default=0.0)
        ok = not problems and not missing
        summary = (f"{'PASS' if ok else 'FAIL'}: {len(reported)}/{len(everyone)} bots reported"
                   + (f" (no result from {', '.join(missing)})" if missing else "")
                   + (f"; left out: {', '.join(sorted(self.skipped))}" if self.skipped else "")
                   + f"; slowest burst of {BURST} lines {slowest:.1f}s"
                   + (f"; {len(problems)} problem(s): " + "; ".join(problems[:6])
                      if problems else ""))
        return {"run": state.run, "ok": ok, "summary": summary, "problems": problems,
                "missing": missing, "results": state.results}

    def _write(self, run: str, report: Dict[str, Any]) -> None:
        try:
            from swarmly.telegram.poll import state_dir
            path = state_dir(self.channel.config) / f"storetest-{run}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        except Exception:                              # noqa: BLE001
            logger.warning("store test %s: the report could not be written", run,
                           exc_info=True)
