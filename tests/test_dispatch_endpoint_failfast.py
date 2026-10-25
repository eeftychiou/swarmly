"""Issue #85 mode 5: the dispatch path must reach the endpoint, or fail fast.

The defect these pin, measured on this host on 2026-09-21:

* a Task Scheduler-launched transport has no ``ANTIGRAVITY_LS_ADDRESS``, so
  ``agentapi send-message`` exits 1 with
  ``{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}``;
* the dispatch then took the blocking ``agy -p`` path, whose budget is
  ``timeout_seconds`` (1500s), while holding ``channel._turn_lock``;
* 15 addressed messages were dropped at ``channel.py:1021`` and 11 ``agentapi
  queue failed`` warnings were logged, while ``heartbeat-check`` reported
  ``fresh beat, age=27s`` -- i.e. healthy.

So there are two independent failures to prevent, and each needs its own guard:

1. **the endpoint is never injected**, so the fast path can never work from a
   scheduler-launched process (the original outage);
2. **the blocking fallback is taken anyway**, so one bad endpoint stalls the
   transport for 25 minutes and discards everything that arrives while it does
   (the amplification that turned one failure into 15).

Test kinds follow the house convention in ``test_agentapi_endpoint_probe.py``:
**discriminating** tests fail on a tree without the fix; **invariant** tests
pass either way on purpose, guarding a property that must hold regardless.
"""

import asyncio
import os
import subprocess
import unittest
from unittest.mock import patch

from swarmly.telegram import endpoint
from swarmly.telegram.adapters.antigravity import (
    AntigravityThreadAdapter,
    is_endpoint_failure,
)
from swarmly.telegram.adapters.base import IncomingMessage

CONV = "11111111-2222-4333-8444-555555555555"

#: The exact payload the live host returned while the endpoint was unknown.
MISSING_VAR = '{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}'


class _ScrubbedEndpointEnv(unittest.TestCase):
    """Base class that removes the ambient endpoint variable for a test.

    A shell spawned BY the IDE inherits ``ANTIGRAVITY_LS_ADDRESS`` (measured on
    this host: ``localhost:62934``), and that is exactly the variable the
    scheduler-launched transport LACKS. Leaving it in place short-circuits
    discovery, so a test meant to exercise the fix would silently assert against
    the ambient value instead -- passing or failing for the wrong reason.
    """

    def setUp(self):
        self._saved = os.environ.pop(endpoint.LS_ADDRESS_ENV, None)
        endpoint.invalidate_cache()

    def tearDown(self):
        if self._saved is not None:
            os.environ[endpoint.LS_ADDRESS_ENV] = self._saved
        endpoint.invalidate_cache()


class DispatchEndpointTests(_ScrubbedEndpointEnv):
    """The resolved endpoint must actually reach the agentapi child process."""

    def _adapter(self, *, allow_blocking=True):
        settings = AntigravityThreadAdapter.default_settings(cwd=".", conversation=CONV)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation = lambda: (CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        adapter.sync_conversation_to_ide = lambda cid: None
        adapter.allow_blocking_fallback = allow_blocking
        return adapter

    def test_the_discovered_endpoint_is_injected_into_the_child_environment(self):
        """DISCRIMINATING: this is the fix; computing the address is not enough.

        An earlier draft resolved the port and then passed ``self.environment()``
        to the child anyway -- the address would have been discovered and
        discarded, and agentapi would still have failed with the very error the
        change exists to remove. Asserting on the CHILD's env is the only way to
        catch that.
        """
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="{}", stderr="")

        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=found), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(self._adapter().respond(
                IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNone(reply, "a queued message returns None by contract")
        self.assertEqual(captured["env"].get(endpoint.LS_ADDRESS_ENV), "127.0.0.1:62934")

    def test_an_ide_set_endpoint_is_not_overwritten(self):
        """INVARIANT: the operator's own value wins.

        A transport launched from inside the IDE inherits a correct address and
        must keep it, even if discovery would have offered something else.
        """
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="{}", stderr="")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.dict("os.environ", {endpoint.LS_ADDRESS_ENV: "127.0.0.1:1111"}), \
             patch.object(endpoint, "discover") as discover_mock, \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            asyncio.run(self._adapter().respond(
                IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertEqual(captured["env"].get(endpoint.LS_ADDRESS_ENV), "127.0.0.1:1111")
        discover_mock.assert_not_called()


class DispatchCredentialTests(_ScrubbedEndpointEnv):
    """The resolved credential must reach the child too.

    An address without the credential authenticates nothing: on the live host
    every dispatch answered ``missing CSRF token`` while the address was
    correct. The probe-side injection is pinned in ``test_endpoint_discovery``;
    this pins the half that production actually dispatches with.
    """

    #: DISTINCT values, not any host's: a sibling test was once vacuous because
    #: its constant happened to equal the live host's ambient token.
    TOKEN = "5f2d1c7a-9b3e-4d08-8a61-2c9e7f4b0d13"
    INHERITED = "8a11bb22-cc33-dd44-ee55-ff6677889900"

    def setUp(self):
        super().setUp()
        self._saved_token = os.environ.pop(endpoint.CSRF_TOKEN_ENV, None)

    def tearDown(self):
        if self._saved_token is not None:
            os.environ[endpoint.CSRF_TOKEN_ENV] = self._saved_token
        super().tearDown()

    def _adapter(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=".", conversation=CONV)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation = lambda: (CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        adapter.sync_conversation_to_ide = lambda cid: None
        adapter.allow_blocking_fallback = False
        return adapter

    def _dispatch_env(self, probe):
        """Drive one dispatch and return the env the agentapi child received."""
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                               stdout="{}", stderr="")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=probe), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(self._adapter().respond(
                IncomingMessage(text="ping", author_alias="Hermes")))
        self.assertIsNone(reply, "a queued message returns None by contract")
        return captured["env"]

    def test_the_resolved_credential_is_injected_into_the_child_environment(self):
        """DISCRIMINATING: this is the half of the fix that authenticates.

        Deleting the ``CSRF_TOKEN_ENV`` injection from ``_dispatch_env`` used to
        leave the full suite green (measured on two hosts): discovery could
        resolve the credential and the child still got neither variable. Only an
        assertion on the CHILD's env can see the difference.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       csrf_token=self.TOKEN, source="discovery",
                                       latency_ms=97)
        env = self._dispatch_env(found)

        self.assertEqual(env.get(endpoint.CSRF_TOKEN_ENV), self.TOKEN)
        self.assertEqual(env.get(endpoint.LS_ADDRESS_ENV), "127.0.0.1:62934")

    def test_an_inherited_credential_is_kept_when_discovery_offers_none(self):
        """INVARIANT: the one caller that previously worked must keep working.

        A transport spawned by the IDE inherits a working credential; a probe
        that resolved an address without one must not blank that value out.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        with patch.dict("os.environ", {endpoint.CSRF_TOKEN_ENV: self.INHERITED}):
            env = self._dispatch_env(found)

        self.assertEqual(env.get(endpoint.CSRF_TOKEN_ENV), self.INHERITED)


class FailFastTests(_ScrubbedEndpointEnv):
    """An unreachable endpoint must not buy a 1500s blocking run."""

    def _adapter(self, *, allow_blocking):
        settings = AntigravityThreadAdapter.default_settings(cwd=".", conversation=CONV)
        # A small budget would hide the bug this pins, so keep the real 1500s.
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation = lambda: (CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        adapter.sync_conversation_to_ide = lambda cid: None
        adapter.allow_blocking_fallback = allow_blocking
        return adapter

    def _run_dispatch(self, *, allow_blocking):
        """Drive one dispatch where agentapi refuses and no endpoint resolves."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "send-message" in argv:
                return subprocess.CompletedProcess(args=argv, returncode=1,
                                                   stdout="", stderr=MISSING_VAR)
            raise AssertionError(f"the blocking fallback was invoked: {argv}")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(self._adapter(allow_blocking=allow_blocking).respond(
                IncomingMessage(text="ping", author_alias="Hermes")))
        return reply, calls

    def test_an_unreachable_endpoint_fails_fast_and_visibly(self):
        """DISCRIMINATING: no blocking run; a named, actionable error instead.

        Pre-fix this call descended into ``agy -p`` for the full budget while
        holding the turn lock. The assertion that matters is that the fallback
        argv was never built -- ``fake_run`` raises if it is.
        """
        reply, calls = self._run_dispatch(allow_blocking=False)

        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("could not reach the live session endpoint", reply.error)
        self.assertIn(endpoint.LS_ADDRESS_ENV, reply.error)
        # Exactly ONE agentapi attempt, and no fallback attempt. This was 2 until
        # the retry learned to stop: with nothing resolved and recovery not
        # erroring, attempt 2 re-resolved the same None from the negative cache
        # and ran byte-identical argv, so it could only repeat attempt 1's exit
        # code -- a second process per message for no information. The retry is
        # still exercised where it can differ, by
        # test_a_stale_endpoint_is_re_discovered_and_the_retry_succeeds.
        self.assertEqual(sum(1 for c in calls if "send-message" in c), 1)
        self.assertTrue(all("send-message" in c for c in calls))

    def test_a_headless_host_keeps_the_documented_fallback(self):
        """INVARIANT: the class contract says an absent endpoint falls back.

        The class docstring documents ``agy -p`` as the supported headless/CI
        path. The fail-fast guard must therefore be gated on BOTH the message
        shape and the caller's policy -- otherwise this change would silently
        break headless delivery, which is a worse bug than the one it fixes.
        """
        invoked = []

        def fake_run(argv, **kwargs):
            invoked.append(list(argv))
            if "send-message" in argv:
                return subprocess.CompletedProcess(args=argv, returncode=1,
                                                   stdout="", stderr=MISSING_VAR)
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                               stdout="done", stderr="")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(self._adapter(allow_blocking=True).respond(
                IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNone(reply, "the fallback succeeded, so the message was delivered")
        self.assertTrue(any("agy" == c[0] for c in invoked),
                        "the headless host must still reach agy -p")

    def test_an_ordinary_agentapi_rejection_still_falls_back(self):
        """INVARIANT: only ENDPOINT-shaped refusals skip the fallback.

        ``no active session`` is agentapi working correctly; the blocking path is
        still the right (if slow) answer there, and must not be short-circuited by
        a classifier that is too eager.
        """
        invoked = []

        def fake_run(argv, **kwargs):
            invoked.append(list(argv))
            if "send-message" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stdout="",
                    stderr='{"error": "no active session"}')
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                               stdout="done", stderr="")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(self._adapter(allow_blocking=False).respond(
                IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNone(reply)
        self.assertTrue(any("agy" == c[0] for c in invoked),
                        "a non-endpoint refusal must keep the fallback")


class RetryOnStaleEndpointTests(_ScrubbedEndpointEnv):
    """A stale port must not permanently wedge delivery."""

    def test_a_stale_endpoint_is_re_discovered_and_the_retry_succeeds(self):
        """DISCRIMINATING: the port belongs to a process that can restart.

        The first attempt fails against a stale port and reports an endpoint
        cause; the adapter must drop the cached address, re-discover, and try
        once more. Without the retry the window between an IDE restart and the
        next transport restart is a delivery outage.
        """
        settings = AntigravityThreadAdapter.default_settings(cwd=".", conversation=CONV)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation = lambda: (CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        adapter.sync_conversation_to_ide = lambda cid: None

        stale = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=90)
        fresh = endpoint.EndpointProbe(address="127.0.0.1:64101", port="64101",
                                       source="discovery", latency_ms=95)
        seen_envs = []

        def fake_run(argv, **kwargs):
            env = kwargs.get("env") or {}
            seen_envs.append(env.get(endpoint.LS_ADDRESS_ENV))
            if "send-message" not in argv:
                raise AssertionError(f"unexpected fallback: {argv}")
            if len(seen_envs) == 1:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stdout="",
                    stderr='{"error": "rpc error: code = Unavailable desc = '
                           'connection error: dial tcp 127.0.0.1:62934"}')
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                               stdout="{}", stderr="")

        # First resolve returns the stale endpoint; the post-failure resolve
        # returns the fresh one.
        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", side_effect=[stale, fresh]), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(adapter.respond(
                IncomingMessage(text="ping", author_alias="Hermes")))

        self.assertIsNone(reply, "the retry delivered, so the message was queued")
        self.assertEqual(seen_envs, ["127.0.0.1:62934", "127.0.0.1:64101"])


if __name__ == "__main__":
    unittest.main()


class TransportGateArmingTests(_ScrubbedEndpointEnv):
    """Issue #123: the gate must be ARMED for the transport, and ONLY there.

    Discriminating: before this wiring no call site set ``allow_blocking_fallback``
    False, so the fail-fast branch was unreachable in production (measured
    2026-09-21 -- the 25-minute blackout arrived through the un-gated path).
    Invariant: headless/one-shot construction keeps the documented fallback, so CI
    and a tokenless ``bus-poll`` cannot regress from this change.
    """

    def _config(self):
        from swarmly.telegram.config import AgentConfig
        return AgentConfig.from_dict({
            "agent": {"alias": "Antigravity", "name": "agy_probe_bot"},
            "adapter": {"type": "antigravity_thread", "conversation": CONV, "cwd": "."},
        })

    def test_transport_build_arms_the_gate(self):
        from swarmly.telegram.adapters import build_adapter
        adapter = build_adapter(self._config(), transport=True)
        self.assertIsInstance(adapter, AntigravityThreadAdapter)
        self.assertFalse(adapter.allow_blocking_fallback)

    def test_headless_build_keeps_the_blocking_fallback(self):
        from swarmly.telegram.adapters import build_adapter
        adapter = build_adapter(self._config())
        self.assertTrue(adapter.allow_blocking_fallback)

    def test_channel_transport_flag_reaches_the_adapter(self):
        from swarmly.telegram.channel import AgentChannel
        self.assertFalse(AgentChannel(self._config(), transport=True)
                         .adapter.allow_blocking_fallback)
        self.assertTrue(AgentChannel(self._config()).adapter.allow_blocking_fallback)

    def test_armed_adapter_from_transport_build_actually_fails_fast(self):
        """M2-with-wiring: the mutation 'remove the fail-fast gate' must make THIS
        test fail. It drives a real dispatch through an adapter armed ONLY by
        ``build_adapter(transport=True)`` -- an armed-but-untested gate is exactly
        as inert as an unarmed one."""
        from swarmly.telegram.adapters import build_adapter
        adapter = build_adapter(self._config(), transport=True)
        adapter.resolve_conversation = lambda: (CONV, "Test")
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"
        adapter.sync_conversation_to_cli = lambda cid: None
        adapter.sync_conversation_to_ide = lambda cid: None
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "send-message" in argv:
                return subprocess.CompletedProcess(args=argv, returncode=1,
                                                   stdout="", stderr=MISSING_VAR)
            raise AssertionError(f"the blocking fallback was invoked: {argv}")

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                   return_value="/bin/ls"), \
             patch.object(endpoint, "discover", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                   side_effect=fake_run):
            reply = asyncio.run(adapter.respond(
                IncomingMessage(text="ping", author_alias="Hermes")))
        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("could not reach the live session endpoint", reply.error)
        self.assertTrue(all("send-message" in c for c in calls))
