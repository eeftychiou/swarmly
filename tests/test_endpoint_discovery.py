"""Regression tests for issue #85 mode 5: recover the live session endpoint.

The defect these pin, measured on this host on 2026-09-21:

* a transport launched by Task Scheduler has no ``ANTIGRAVITY_LS_ADDRESS``;
* ``resolve_agentapi()`` still SUCCEEDS (the binary is present), so the adapter
  cannot tell "headless, no IDE" from "IDE present, endpoint unknown";
* every ``send-message`` therefore exits 1 with
  ``{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}``;
* every addressed message fell through to a blocking ``agy -p`` run whose
  1500s budget held the channel's turn lock -- 15 ``turn already in progress``
  drops and 11 ``agentapi queue failed`` warnings in one afternoon, while
  ``heartbeat-check`` reported ``fresh beat, age=27s`` (healthy).

The port IS discoverable: ``language_server`` announces it in its own banner
(measured: ``62933`` gRPC, ``62934`` HTTP; only 62934 answers).

Test kinds, in the house style of ``test_agentapi_endpoint_probe.py``:

* **discriminating** -- would fail on a tree without endpoint recovery;
* **invariant** -- passes either way on purpose, guarding a property that must
  hold regardless (a headless host staying a clean no-op; a wrong port never
  being accepted; the reason never carrying a token).
"""

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from swarmly.telegram import endpoint
from swarmly.telegram.endpoint import (
    candidate_ports,
    discover,
    pids_from_tasklist,
    ports_from_banner,
    ports_from_netstat,
    resolve_ls_address,
)


# Verbatim from the live host's %APPDATA%\Antigravity\logs\language_server.log.
# Note the gRPC line comes FIRST and the HTTP line second -- the ranking below is
# what stops the wrong one being picked, not the file order.
LIVE_BANNER = (
    "ERROR: logging before google.Init: I0921 14:19:24.232884       1 server.go:628] "
    "Language server listening on random port at 62933 for HTTPS (gRPC)\n"
    "ERROR: logging before google.Init: I0921 14:19:24.232884       1 server.go:636] "
    "Language server listening on random port at 62934 for HTTP\n"
)

# A second run appended to the same log: earlier ports are now dead.
BANNER_AFTER_RESTART = LIVE_BANNER + (
    "ERROR: logging before google.Init: I0921 16:40:01.100000       1 server.go:628] "
    "Language server listening on random port at 64100 for HTTPS (gRPC)\n"
    "ERROR: logging before google.Init: I0921 16:40:01.100000       1 server.go:636] "
    "Language server listening on random port at 64101 for HTTP\n"
)

# Verbatim shape of `netstat -ano -p TCP` on this host (pid 33416 = language_server).
LIVE_NETSTAT = """
  TCP    127.0.0.1:62933        0.0.0.0:0              LISTENING       33416
  TCP    127.0.0.1:62934        0.0.0.0:0              LISTENING       33416
  TCP    127.0.0.1:49700        0.0.0.0:0              LISTENING       99999
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    [::1]:52000            0.0.0.0:0              LISTENING       33416
  TCP    127.0.0.1:52001        0.0.0.0:0              ESTABLISHED     33416
"""

LIVE_TASKLIST = (
    '"language_server.exe","33416","Console","1","350,000 K"\n'
    '"Antigravity.exe","30932","Console","1","120,000 K"\n'
)

CONV = "11111111-2222-4333-8444-555555555555"


class BannerParsingTests(unittest.TestCase):
    def test_http_port_outranks_its_grpc_sibling(self):
        """DISCRIMINATING: the gRPC port appears first in the log and refuses.

        Both bind loopback and accept a TCP connection; only HTTP serves
        agentapi. Ranking by file order would pick 62933 and fail every probe.
        """
        ports = ports_from_banner(LIVE_BANNER)
        self.assertEqual(ports[0], "62934")
        self.assertIn("62933", ports)

    def test_latest_announcement_wins_after_a_restart(self):
        """DISCRIMINATING: the banner is appended to, so old ports are dead.

        Reporting a port from a previous IDE run would hand the delivery path an
        address that cannot work -- and it would look like a fresh discovery.
        """
        ports = ports_from_banner(BANNER_AFTER_RESTART)
        self.assertEqual(ports[0], "64101")
        self.assertEqual(ports[1], "64100")

    def test_unrelated_log_noise_yields_no_ports(self):
        """INVARIANT: a host without the banner must not invent a candidate."""
        self.assertEqual(ports_from_banner("nothing to see here\n"), [])
        self.assertEqual(ports_from_banner(""), [])


class NetstatParsingTests(unittest.TestCase):
    def test_only_loopback_listeners_of_the_language_server(self):
        """DISCRIMINATING: filter by BOTH pid and loopback address family.

        An unfiltered scan would offer ports owned by unrelated services (and a
        non-loopback bind is not something agentapi should be pointed at).
        """
        ports = ports_from_netstat(LIVE_NETSTAT, [33416])
        self.assertEqual(ports, ["62933", "62934", "52000"])

    def test_established_and_foreign_pids_are_excluded(self):
        """INVARIANT: a connected socket is not a listener; another pid is not ours."""
        ports = ports_from_netstat(LIVE_NETSTAT, [33416])
        self.assertNotIn("52001", ports)      # ESTABLISHED, not LISTENING
        self.assertNotIn("49700", ports)      # LISTENING but pid 99999
        self.assertNotIn("135", ports)        # LISTENING but not loopback

    def test_no_pids_means_no_ports(self):
        """INVARIANT: without a pid there is nothing to attribute a port to."""
        self.assertEqual(ports_from_netstat(LIVE_NETSTAT, []), [])


class TasklistParsingTests(unittest.TestCase):
    def test_extracts_the_language_server_pid_only(self):
        """INVARIANT: sibling IDE processes must not be mistaken for the server."""
        self.assertEqual(pids_from_tasklist(LIVE_TASKLIST), [33416])

    def test_no_match_is_empty(self):
        self.assertEqual(pids_from_tasklist("INFO: No tasks are running\n"), [])


class CandidateAssemblyTests(unittest.TestCase):
    def test_banner_leads_and_netstat_dedupes(self):
        """DISCRIMINATING: both sources agree here, and the result must not repeat.

        A duplicated candidate would double every probe (the gRPC port costs a
        failed round trip each time).
        """
        ports = candidate_ports(log_text=LIVE_BANNER, netstat_text=LIVE_NETSTAT,
                                pids=[33416])
        self.assertEqual(ports[0], "62934")
        self.assertEqual(len(ports), len(set(ports)))
        # 52000 is LISTENING on loopback for the language server pid but appears
        # in no banner line, so it can only have come from the netstat tier.
        self.assertEqual(ports[-1], "52000")

    def test_netstat_contributes_when_the_log_is_gone(self):
        """DISCRIMINATING: a rotated-away log must not make discovery blind."""
        ports = candidate_ports(log_text="", netstat_text=LIVE_NETSTAT, pids=[33416])
        self.assertEqual(set(ports), {"62933", "62934", "52000"})

    def test_host_log_read_reaches_the_head_window(self):
        """DISCRIMINATING: with ``log_text`` unset the host read must hit the HEAD.

        This guards the WIRING, not the reader -- ``log_text_from_log`` can be
        perfect and still be bypassed. Measured defect it pins: the live log's only
        two announcements sit at lines 5-6 of a 12MB file, so a tail-only host read
        returned ``[]`` and the banner tier was silently dead, leaving discovery to
        netstat alone (which ranks the gRPC port first).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "language_server.log"
            log.write_text(LIVE_BANNER + ("noise\n" * 60000), encoding="utf-8")
            with patch.object(endpoint, "language_server_log_path",
                              return_value=log):
                ports = candidate_ports(netstat_text="", pids=[])

        self.assertEqual(ports[0], "62934",
                         "the banner tier must survive the host read")

    def test_no_sources_yields_nothing(self):
        """INVARIANT: a headless host produces no candidates, not an exception."""
        self.assertEqual(candidate_ports(log_text="", netstat_text="", pids=[]), [])


class ProbeTests(unittest.TestCase):
    def _completed(self, rc, out):
        return subprocess.CompletedProcess(args=["ls"], returncode=rc, stdout=out, stderr="")

    def test_success_requires_rc_zero_AND_the_metadata_body(self):
        """DISCRIMINATING: rc=0 alone is not evidence of a usable endpoint."""
        with patch.object(endpoint.subprocess, "run",
                          return_value=self._completed(0, '{"response":{"conversationMetadata":{}}}')):
            ok, detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934")
        self.assertTrue(ok, detail)

    def test_rc_zero_without_metadata_is_a_failure(self):
        """INVARIANT: guard against a future agentapi printing an unrelated ok body."""
        with patch.object(endpoint.subprocess, "run",
                          return_value=self._completed(0, '{"response":{"other":{}}}')):
            ok, _detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934")
        self.assertFalse(ok)

    def test_the_probe_sets_the_address_it_is_testing(self):
        """DISCRIMINATING: the probe must set the var, else it tests nothing.

        Every candidate would 'fail' identically and discovery would always
        report nothing -- a silent regression that still exits cleanly.
        """
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return self._completed(0, '{"conversationMetadata":true}')

        with patch.object(endpoint.subprocess, "run", side_effect=fake_run):
            endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934")

        self.assertEqual(captured["env"].get(endpoint.LS_ADDRESS_ENV), "127.0.0.1:62934")

    def test_a_hung_probe_is_bounded_and_never_raises(self):
        """INVARIANT: a wedged socket must not stall the delivery path."""
        with patch.object(endpoint.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="ls", timeout=5)):
            ok, detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934",
                                             timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_oserror_is_reported_not_raised(self):
        """INVARIANT: discovery failing is an ordinary outcome."""
        with patch.object(endpoint.subprocess, "run", side_effect=OSError("boom")):
            ok, detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934")
        self.assertFalse(ok)
        self.assertIn("OSError", detail)


class DiscoverTests(unittest.TestCase):
    def test_returns_the_first_answering_port(self):
        """DISCRIMINATING: 62933 (gRPC sibling) fails, 62934 (HTTP) answers.

        This is the measured host behaviour, and it is why the result is the
        second candidate rather than the first.
        """
        def fake_probe(_cmd, _conv, port, timeout=5.0, **_kwargs):
            # ``**_kwargs`` absorbs ``csrf_token``: discovery now tries the
            # caller's own (absent) credential first and only then reads one off
            # the live process. This test pins PORT RANKING, so the first pass
            # answering 62934 is exactly the behaviour under test.
            return (port == "62934"), f"rc={0 if port == '62934' else 1}", 90

        with patch.object(endpoint, "probe", side_effect=fake_probe):
            found = discover(["/bin/ls", "agentapi"], CONV,
                             log_text=LIVE_BANNER, netstat_text="", pids=[])

        self.assertIsNotNone(found)
        self.assertEqual(found.port, "62934")
        self.assertEqual(found.address, "127.0.0.1:62934")

    def test_no_answering_port_is_none_not_an_exception(self):
        """INVARIANT: a host with no live IDE must degrade quietly."""
        with patch.object(endpoint, "probe", return_value=(False, "rc=1", 5)):
            found = discover(["/bin/ls", "agentapi"], CONV, log_text=LIVE_BANNER,
                             netstat_text="", pids=[])
        self.assertIsNone(found)

    def test_missing_agentapi_or_conversation_short_circuits(self):
        """INVARIANT: nothing to probe with means no probing, and no subprocess."""
        with patch.object(endpoint, "probe") as probe_mock:
            self.assertIsNone(discover(None, CONV))
            self.assertIsNone(discover(["/bin/ls", "agentapi"], None))
        probe_mock.assert_not_called()


class ResolveLsAddressTests(unittest.TestCase):
    def setUp(self):
        endpoint.invalidate_cache()

    def tearDown(self):
        endpoint.invalidate_cache()

    def test_an_operator_set_variable_is_respected_without_probing(self):
        """DISCRIMINATING: an explicit choice must win, and must cost nothing.

        A transport launched from inside the IDE inherits a correct address; it
        must not pay a probe per message for a value it already has.
        """
        with patch.object(endpoint, "discover") as discover_mock:
            value = resolve_ls_address(["/bin/ls", "agentapi"], CONV,
                                       environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
        self.assertEqual(value, "127.0.0.1:1234")
        discover_mock.assert_not_called()

    def test_discovery_fills_the_gap_and_is_cached(self):
        """DISCRIMINATING: the actual fix -- a scheduler-launched run recovers.

        The second call must NOT re-probe: discovery runs on the delivery path,
        and probing per message would recreate the latency this removes.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        with patch.object(endpoint, "discover", return_value=found) as discover_mock:
            first = resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
            second = resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})

        self.assertEqual(first, "127.0.0.1:62934")
        self.assertEqual(second, "127.0.0.1:62934")
        self.assertEqual(discover_mock.call_count, 1, "discovery must be cached")

    def test_no_environment_and_no_discovery_is_none(self):
        """INVARIANT: the supported headless case stays a clean no-op.

        The class docstring treats an absent agentapi as a supported CI shape, so
        recovery failing must return None rather than raising or logging loudly.
        """
        with patch.object(endpoint, "discover", return_value=None):
            self.assertIsNone(resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={}))

    def test_invalidate_forces_a_fresh_probe(self):
        """DISCRIMINATING: the port belongs to a process that can restart.

        Without invalidation a stale port would be reused for the whole run,
        which is the failure mode this module exists to prevent.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        with patch.object(endpoint, "discover", return_value=found) as discover_mock:
            resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
            endpoint.invalidate_cache()
            resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
        self.assertEqual(discover_mock.call_count, 2)

    def test_blank_variable_is_treated_as_unset(self):
        """INVARIANT: an empty value in an env file must not short-circuit discovery.

        ``FOO=`` in a .env file is common; treating it as a choice would leave
        the host wedged with a variable that looks set.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        with patch.object(endpoint, "discover", return_value=found):
            value = resolve_ls_address(["/bin/ls", "agentapi"], CONV,
                                       environ={endpoint.LS_ADDRESS_ENV: "   "})
        self.assertEqual(value, "127.0.0.1:62934")


class BoundedReadTests(unittest.TestCase):
    def test_only_the_tail_is_read(self):
        """DISCRIMINATING: the live log is ~9.5MB; a full read per message is a bug.

        The banner we need is appended at the END, so bounding the read costs
        nothing and removes a per-dispatch multi-megabyte read. The window here
        is deliberately narrower than the filler, so the superseded banner
        genuinely falls outside it -- otherwise the assertion would pass on a
        full read too.
        """
        import tempfile

        new_banner = BANNER_AFTER_RESTART[len(LIVE_BANNER):]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "language_server.log"
            path.write_text(LIVE_BANNER + ("noise\n" * 200000) + new_banner,
                            encoding="utf-8")
            text = endpoint._read_text(path, max_bytes=400)

        self.assertLess(len(text), 401, "tail read must be bounded")
        ports = ports_from_banner(text)
        self.assertEqual(ports[0], "64101", "the current banner must survive the bound")
        self.assertNotIn("62934", ports, "the superseded banner must be outside the window")

    def test_small_file_is_read_whole(self):
        """INVARIANT: bounding must not truncate a log shorter than the window."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.log"
            path.write_text(LIVE_BANNER, encoding="utf-8")
            text = endpoint._read_text(path, max_bytes=8192)
        self.assertEqual(ports_from_banner(text)[0], "62934")

    def test_missing_file_is_empty_not_an_error(self):
        """INVARIANT: discovery on a host with no log must not raise."""
        self.assertEqual(endpoint._read_text(Path("C:/definitely/not/here.log")), "")


class LogWindowTests(unittest.TestCase):
    """The banner lives at the log HEAD, so a tail-only read finds nothing.

    Regression for the defect issue #126 Defect 1 identified but the credential
    fix did not close: the live log is append-only, the banner is written when the
    process starts, and the bounded tail skipped it entirely. Measured on this
    host: the only two announcements were at lines 5-6 of a 12MB file, while the
    256KB tail window began ~992KB in. ``ports_from_banner()`` returned ``[]``.
    """

    #: Filler large enough to push the banner outside a tail-only window.
    FILLER = "noise\n" * 60000          # ~360KB, comfortably over _MAX_LOG_TAIL_BYTES

    def _log_with_banner_at_head(self, tmp: str, banner: str = LIVE_BANNER) -> Path:
        path = Path(tmp) / "language_server.log"
        path.write_text(banner + self.FILLER, encoding="utf-8")
        return path

    def test_head_window_reaches_a_banner_the_tail_cannot(self):
        """DISCRIMINATING: the live layout is banner-first, filler-after.

        Fails on a tail-only reader -- which is exactly the shipped defect: the
        banner is outside the tail window, so no port is announced at all.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._log_with_banner_at_head(tmp)
            tail_only = endpoint._read_text(path)
            text = endpoint.log_text_from_log(path)

        self.assertEqual(ports_from_banner(tail_only), [],
                         "precondition: the tail alone must miss the banner here")
        self.assertEqual(ports_from_banner(text)[0], "62934",
                         "the head window must recover the live HTTP port")

    def test_head_and_tail_are_concatenated_in_chronological_order(self):
        """DISCRIMINATING: newest-first ranking needs head BEFORE tail.

        ``ports_from_banner`` reverses the match list, so it only names the live
        port if the newest announcement sorts last in the concatenated text.
        Reversing the concatenation (tail + head) silently promotes a dead port.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "language_server.log"
            path.write_text(LIVE_BANNER + self.FILLER
                            + BANNER_AFTER_RESTART[len(LIVE_BANNER):],
                            encoding="utf-8")
            text = endpoint.log_text_from_log(path)

        # Chronological: head's earliest announcement must precede the tail's.
        self.assertLess(text.index("62934"), text.index("64101"),
                        "head must be concatenated before tail")
        ports = ports_from_banner(text)
        self.assertEqual(ports[0], "64101", "the newest spawn wins")
        self.assertIn("62934", ports, "the earlier spawn is still a candidate")

    def test_short_log_is_read_once_and_unbounded_by_the_head_window(self):
        """INVARIANT: a file under the head window is never split or truncated."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.log"
            path.write_text(LIVE_BANNER, encoding="utf-8")
            text = endpoint.log_text_from_log(path)

        # Exactly one copy: a split read would duplicate the head lines.
        self.assertEqual(text.count("listening on random port"), 2)
        self.assertEqual(ports_from_banner(text)[0], "62934")

    def test_missing_path_yields_empty_text(self):
        """INVARIANT: the head reader must not raise on an absent/unset log."""
        self.assertEqual(endpoint.log_text_from_log(None), "")
        self.assertEqual(
            endpoint.log_text_from_log(Path("C:/definitely/not/here.log")), "")


class NegativeCacheTests(unittest.TestCase):
    """A failed discovery must not be retried on every single message."""

    def setUp(self):
        endpoint.invalidate_cache()

    def tearDown(self):
        endpoint.invalidate_cache()

    def test_repeat_failures_do_not_re_probe_immediately(self):
        """DISCRIMINATING: a headless host must not pay a probe per message.

        Without the negative cache, every message on a host with no IDE pays a
        netstat call plus a probe per candidate -- a permanent tax for a
        condition that is simply "no IDE here".
        """
        with patch.object(endpoint, "discover", return_value=None) as discover_mock:
            first = resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
            second = resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(discover_mock.call_count, 1, "the negative result must be cached")

    def test_a_failure_clears_the_negative_record(self):
        """DISCRIMINATING: a delivery failure is evidence the situation changed."""
        with patch.object(endpoint, "discover", return_value=None):
            resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
        endpoint.invalidate_cache()
        with patch.object(endpoint, "discover", return_value=None) as discover_mock:
            resolve_ls_address(["/bin/ls", "agentapi"], CONV, environ={})
        self.assertEqual(discover_mock.call_count, 1, "invalidate must force a re-probe")


class EndpointFailureClassifierTests(unittest.TestCase):
    """The classifier decides whether the blocking fallback is pathological.

    It gates on the message but the CALLER gates on ``allow_blocking_fallback``,
    so the two must stay distinguishable: widening this too far would turn a
    supported headless host into a host that never delivers.
    """

    def test_the_measured_missing_variable_message_is_recognised(self):
        """DISCRIMINATING: the exact string from the live host."""
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertTrue(is_endpoint_failure("ANTIGRAVITY_LS_ADDRESS is not set"))

    def test_the_measured_unreachable_endpoint_message_is_recognised(self):
        """DISCRIMINATING: the sibling gRPC port produced this exact form."""
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertTrue(is_endpoint_failure(
            "rpc error: code = Unavailable desc = connection error: "
            "dial tcp 127.0.0.1:62933: connectex: No connection could be made"))

    def test_an_ordinary_rejection_is_not_an_endpoint_failure(self):
        """INVARIANT: a session-level refusal must keep using the fallback.

        ``no active session`` is agentapi working correctly and reporting a
        business condition -- not an endpoint problem.
        """
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertFalse(is_endpoint_failure("no active session"))
        self.assertFalse(is_endpoint_failure(""))
        self.assertFalse(is_endpoint_failure("exit code 1"))

    def test_matching_is_case_and_whitespace_insensitive(self):
        """INVARIANT: the payload's formatting is not ours to control."""
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertTrue(is_endpoint_failure("Antigravity_LS_Address\n   is not set"))


class LiveHostTests(unittest.TestCase):
    """Opt-in checks against the real host (read-only).

    Skipped unless ``A2A_ENDPOINT_LIVE=1``: they touch the real log and the real
    language_server, so they must never run as part of the ordinary suite on a
    machine that is not the object of the measurement.
    """

    @unittest.skipUnless(os.environ.get("A2A_ENDPOINT_LIVE") == "1",
                         "set A2A_ENDPOINT_LIVE=1 to probe the real host")
    def test_real_host_banner_names_an_http_port(self):
        path = endpoint.language_server_log_path()
        if path is None or not Path(path).is_file():
            self.skipTest("no language server log on this host")
        ports = ports_from_banner(Path(path).read_text(encoding="utf-8", errors="replace"))
        self.assertTrue(ports, "expected at least one announced port")
        # The live host's winner; recorded as the measured expectation.
        self.assertIn("62934", ports)


class CsrfArgumentParsingTests(unittest.TestCase):
    """The credential lives on the live process's own command line.

    Measured on this host 2026-09-21: ``language_server.exe`` is spawned with
    ``--csrf_token <uuid>``, and that is the ONLY place the value exists -- it is
    written to no config file and logged nowhere (69 historical values in the
    IDE's own log, only the newest accepted).
    """

    TOKEN = "00000000-0000-4000-8000-0000000000aa"

    def test_a_space_separated_flag_is_parsed(self):
        """DISCRIMINATING: this is the exact spelling the live host uses."""
        text = f'"C:\\...\\language_server.exe" --standalone --csrf_token {self.TOKEN} --foo'
        self.assertEqual(endpoint.csrf_token_from_command_line(text), self.TOKEN)

    def test_an_equals_separated_flag_is_parsed(self):
        """INVARIANT: the flag parser accepts either form, so a build that
        switches spelling must not silently disable credential recovery."""
        text = f'language_server.exe --csrf_token={self.TOKEN}'
        self.assertEqual(endpoint.csrf_token_from_command_line(text), self.TOKEN)

    def test_the_last_occurrence_wins(self):
        """INVARIANT: mirrors the banner parser's newest-wins rule."""
        first = "00000000-0000-4000-8000-0000000000aa"
        second = "00000000-0000-4000-8000-0000000000bb"
        text = f"--csrf_token {first} --csrf_token {second}"
        self.assertEqual(endpoint.csrf_token_from_command_line(text), second)

    def test_nothing_parseable_is_none_not_an_exception(self):
        """INVARIANT: a process list full of unrelated command lines is normal."""
        self.assertIsNone(endpoint.csrf_token_from_command_line(""))
        self.assertIsNone(endpoint.csrf_token_from_command_line(None))
        self.assertIsNone(endpoint.csrf_token_from_command_line("--csrf_token not-a-uuid"))
        self.assertIsNone(endpoint.csrf_token_from_command_line("--csrf_token"))

    def test_a_host_bridge_token_is_not_mistaken_for_the_session_credential(self):
        """DISCRIMINATING: two DIFFERENT 64-hex credentials exist on this host.

        ``host_bridge_token`` sits in the same command line and is NOT what
        ``agentapi`` authenticates with; handing it over produced
        ``invalid CSRF token``. Pinning the UUID shape is what keeps them apart.
        """
        hex64 = "a" * 64
        self.assertIsNone(endpoint.csrf_token_from_command_line(
            f"--host_bridge_token {hex64}"))


class CredentialHygieneTests(unittest.TestCase):
    """The credential must never reach a log line, a reply, or a repr."""

    TOKEN = "00000000-0000-4000-8000-0000000000aa"

    def test_the_probe_object_never_prints_its_credential(self):
        """DISCRIMINATING: this object is formatted into error paths and logs."""
        probe = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97,
                                       csrf_token=self.TOKEN)
        text = repr(probe)
        self.assertNotIn(self.TOKEN, text, "the credential leaked into repr()")
        self.assertIn("***", text, "the redaction must be visible, not silent")

    def test_an_empty_credential_is_not_reported_as_set(self):
        """INVARIANT: the repr must not claim a credential it does not have."""
        probe = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97)
        self.assertNotIn("***", repr(probe))

    def test_the_probe_detail_never_carries_the_credential(self):
        """DISCRIMINATING: ``detail`` is logged AND surfaced toward Telegram.

        The payload goes on STDOUT because ``probe`` merges the child's stderr
        into it -- putting it on ``stderr`` alone would leave ``output`` empty and
        the assertion would pass without ever exercising the credential branch.
        """
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                args=argv, returncode=1,
                stdout='{"error": "rpc error: code = Unauthenticated desc = '
                       'missing CSRF token"}',
                stderr="")

        with patch.object(endpoint.subprocess, "run", side_effect=fake_run):
            ok, detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934",
                                             csrf_token=self.TOKEN)
        self.assertFalse(ok)
        self.assertNotIn(self.TOKEN, detail)
        # The condition, not the value: this is the actionable part.
        self.assertIn("csrf", detail.lower())

    def test_the_payload_echoed_back_is_not_forwarded_verbatim(self):
        """INVARIANT: a server that echoes the credential must not leak it via us.

        Deliberately adversarial: even if a future agentapi echoes the token in
        its own error body, ``detail`` is ours to shape and must stay
        credential-free.
        """
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                args=argv, returncode=1,
                stdout=f'{{"error": "invalid CSRF token: {self.TOKEN}"}}',
                stderr="")

        with patch.object(endpoint.subprocess, "run", side_effect=fake_run):
            _ok, detail, _ms = endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934",
                                              csrf_token=self.TOKEN)
        self.assertNotIn(self.TOKEN, detail)


class ProbeCredentialInjectionTests(unittest.TestCase):
    """A probe is only evidence if it carries the credential it claims to test."""

    #: A DISTINCT value, not the live host's. Using the host's own token made an
    #: earlier draft of this test vacuous: injection could be removed entirely and
    #: the assertion still passed, because the ambient environment happened to
    #: carry the very value it expected (caught by scratch/mutate_check.py).
    TOKEN = "1234abcd-0000-1111-2222-333344445555"
    INHERITED = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        # ``probe`` copies os.environ, so an ambient credential would mask the
        # injection under test.
        self._saved = os.environ.pop(endpoint.CSRF_TOKEN_ENV, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[endpoint.CSRF_TOKEN_ENV] = self._saved

    def _captured_env(self, **probe_kwargs):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout='{"conversationMetadata":true}',
                stderr="")

        with patch.object(endpoint.subprocess, "run", side_effect=fake_run):
            endpoint.probe(["/bin/ls", "agentapi"], CONV, "62934", **probe_kwargs)
        return captured["env"]

    def test_a_supplied_credential_is_injected(self):
        """DISCRIMINATING: this is the fix; without it every probe is rejected
        with ``missing CSRF token`` and a working endpoint looks dead."""
        env = self._captured_env(csrf_token=self.TOKEN)
        self.assertEqual(env.get(endpoint.CSRF_TOKEN_ENV), self.TOKEN)

    def test_no_credential_leaves_an_inherited_one_untouched(self):
        """INVARIANT: an IDE-spawned caller must keep its own ambient value.

        Overwriting the inherited credential with an empty string would break the
        one process that previously worked.
        """
        with patch.dict("os.environ", {endpoint.CSRF_TOKEN_ENV: self.INHERITED}):
            env = self._captured_env()
        self.assertEqual(env.get(endpoint.CSRF_TOKEN_ENV), self.INHERITED)


class DiscoverCredentialTests(unittest.TestCase):
    """Discovery must recover the credential, and only pay for it when needed."""

    TOKEN = "00000000-0000-4000-8000-0000000000aa"

    def test_an_inherited_credential_is_tried_before_the_host_is_queried(self):
        """DISCRIMINATING (cost): the host query is ~1.2s measured.

        A transport the IDE spawned already holds a working credential, so its
        first pass succeeds and the expensive query must never run. Asserting on
        ``csrf_token_candidates`` being untouched is the only way to pin that --
        a correct result would otherwise look identical.
        """
        with patch.object(endpoint, "probe", return_value=(True, "rc=0", 90)), \
             patch.object(endpoint, "csrf_token_candidates") as tokens_mock:
            found = discover(["/bin/ls", "agentapi"], CONV,
                             log_text=LIVE_BANNER, netstat_text="", pids=[])

        self.assertIsNotNone(found)
        self.assertEqual(found.csrf_token, "")
        tokens_mock.assert_not_called()

    def test_a_failed_first_pass_recovers_the_live_credential(self):
        """DISCRIMINATING: the scheduler-launched case, which is the whole bug.

        Pass 1 has no credential and is refused; pass 2 must find the credential
        on the live process and succeed. Without this the endpoint is discovered
        correctly and still cannot dispatch.
        """
        seen_tokens = []

        def fake_probe(_cmd, _conv, port, timeout=5.0, csrf_token=""):
            seen_tokens.append(csrf_token)
            return (csrf_token == self.TOKEN), "rc=0" if csrf_token else "credential rejected (csrf)", 90

        with patch.object(endpoint, "probe", side_effect=fake_probe), \
             patch.object(endpoint, "csrf_token_candidates", return_value=[self.TOKEN]):
            found = discover(["/bin/ls", "agentapi"], CONV,
                             log_text=LIVE_BANNER, netstat_text="", pids=[])

        self.assertIsNotNone(found)
        self.assertEqual(found.csrf_token, self.TOKEN)
        self.assertEqual(seen_tokens[0], "", "the caller's own env must be tried first")

    def test_no_credential_anywhere_is_still_none_not_an_exception(self):
        """INVARIANT: a host with a port but no recoverable credential must
        degrade quietly rather than raise into the delivery path."""
        with patch.object(endpoint, "probe", return_value=(False, "credential rejected (csrf)", 5)), \
             patch.object(endpoint, "csrf_token_candidates", return_value=[self.TOKEN]):
            found = discover(["/bin/ls", "agentapi"], CONV,
                             log_text=LIVE_BANNER, netstat_text="", pids=[])
        self.assertIsNone(found)


class ResolveEndpointTests(unittest.TestCase):
    """The four tiers, and the caching that keeps the delivery path cheap."""

    TOKEN = "00000000-0000-4000-8000-0000000000aa"

    def setUp(self):
        endpoint.invalidate_cache()

    def tearDown(self):
        endpoint.invalidate_cache()

    def test_a_complete_operator_pair_costs_no_probe(self):
        """DISCRIMINATING: an explicit operator choice must be free."""
        with patch.object(endpoint, "probe") as probe_mock, \
             patch.object(endpoint, "discover") as discover_mock:
            found = endpoint.resolve_endpoint(
                ["/bin/ls", "agentapi"], CONV,
                environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234",
                         endpoint.CSRF_TOKEN_ENV: self.TOKEN})
        self.assertEqual(found.address, "127.0.0.1:1234")
        self.assertEqual(found.csrf_token, self.TOKEN)
        probe_mock.assert_not_called()
        discover_mock.assert_not_called()

    def test_an_address_without_a_credential_has_its_credential_recovered(self):
        """DISCRIMINATING: the measured live-host shape.

        The transport inherited a correct ``ANTIGRAVITY_LS_ADDRESS`` and still
        failed every dispatch with ``missing CSRF token``. The address must be
        honoured, and the credential recovered for it.
        """
        with patch.object(endpoint, "probe", return_value=(True, "rc=0", 80)) as probe_mock, \
             patch.object(endpoint, "csrf_token_candidates", return_value=[self.TOKEN]), \
             patch.object(endpoint, "discover") as discover_mock:
            found = endpoint.resolve_endpoint(
                ["/bin/ls", "agentapi"], CONV,
                environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
        self.assertEqual(found.address, "127.0.0.1:1234")
        self.assertEqual(found.csrf_token, self.TOKEN)
        # Port discovery must NOT run: the operator's address wins.
        discover_mock.assert_not_called()
        self.assertEqual(probe_mock.call_args.args[2], "1234")

    def test_an_unusable_operator_address_is_not_silently_replaced(self):
        """INVARIANT: report the real problem rather than a different endpoint."""
        with patch.object(endpoint, "probe", return_value=(False, "rc=1", 5)), \
             patch.object(endpoint, "csrf_token_candidates", return_value=[self.TOKEN]), \
             patch.object(endpoint, "discover") as discover_mock:
            found = endpoint.resolve_endpoint(
                ["/bin/ls", "agentapi"], CONV,
                environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
        self.assertEqual(found.address, "127.0.0.1:1234")
        self.assertEqual(found.csrf_token, "")
        discover_mock.assert_not_called()

    def test_a_failed_credential_search_is_not_re_run_per_message(self):
        """DISCRIMINATING (cost): the host query is ~1.2s measured.

        A wrong operator address would otherwise re-run PowerShell, and re-probe,
        on every single message -- turning a misconfiguration into a permanent
        per-message tax.
        """
        with patch.object(endpoint, "probe", return_value=(False, "rc=1", 5)), \
             patch.object(endpoint, "csrf_token_candidates",
                          return_value=[self.TOKEN]) as tokens_mock:
            endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV,
                                      environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
            second = endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV,
                                               environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
        self.assertEqual(second.address, "127.0.0.1:1234")
        self.assertEqual(tokens_mock.call_count, 1, "the failed search must be remembered")

    def test_a_discovery_result_is_cached_with_its_credential(self):
        """DISCRIMINATING: one discovery per run, credential included."""
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97,
                                       csrf_token=self.TOKEN)
        with patch.object(endpoint, "discover", return_value=found) as discover_mock:
            first = endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV,
                                              environ={})
            second = endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV,
                                               environ={})
        self.assertEqual(first.csrf_token, self.TOKEN)
        self.assertEqual(second.csrf_token, self.TOKEN)
        self.assertEqual(second.address, "127.0.0.1:62934")
        self.assertEqual(discover_mock.call_count, 1)

    def test_a_failed_discovery_is_not_re_run_per_message(self):
        """DISCRIMINATING: the negative twin of the cache test above.

        The adapter's dispatch retry skips attempt 2 when nothing was resolved
        and recovery did not raise, on the premise that attempt 2 cannot differ
        -- because `resolve_endpoint` re-reads the negative cache rather than the
        host. This test IS that premise.

        It is not academic: if a failed discovery stopped being remembered here,
        attempt 2 would run a fresh `discover()` and could genuinely find an
        endpoint the first attempt missed. The skip would then be a lost
        recovery path rather than a saved process, so this is the assertion that
        keeps the optimisation honest.
        """
        with patch.object(endpoint, "discover", return_value=None) as discover_mock:
            first = endpoint.resolve_endpoint(
                ["/bin/ls", "agentapi"], CONV, environ={})
            second = endpoint.resolve_endpoint(
                ["/bin/ls", "agentapi"], CONV, environ={})

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            discover_mock.call_count, 1,
            "a failed discovery must be remembered for _NEGATIVE_TTL_SECONDS; "
            "the adapter's retry-skip assumes attempt 2 cannot differ because of "
            "this cache")

    def test_an_ambiguous_address_with_nothing_to_probe_with_is_returned_asis(self):
        """INVARIANT: no credential search when there is no way to test one."""
        with patch.object(endpoint, "csrf_token_candidates") as tokens_mock:
            found = endpoint.resolve_endpoint(None, None,
                                              environ={endpoint.LS_ADDRESS_ENV: "127.0.0.1:1234"})
        self.assertEqual(found.address, "127.0.0.1:1234")
        tokens_mock.assert_not_called()

    def test_invalidate_drops_the_cached_credential_too(self):
        """DISCRIMINATING: address and credential come from the same spawn.

        Keeping one and dropping the other would produce a correct port with a
        dead credential -- the exact silent-failure shape this change removes.
        """
        found = endpoint.EndpointProbe(address="127.0.0.1:62934", port="62934",
                                       source="discovery", latency_ms=97,
                                       csrf_token=self.TOKEN)
        with patch.object(endpoint, "discover", return_value=found):
            endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV, environ={})
        endpoint.invalidate_cache()
        with patch.object(endpoint, "discover", return_value=None), \
             patch.object(endpoint, "csrf_token_candidates", return_value=[]):
            self.assertIsNone(endpoint.resolve_endpoint(["/bin/ls", "agentapi"], CONV,
                                                        environ={}))


class EndpointFailureCredentialTests(unittest.TestCase):
    """The classifier asymmetry that made a stale credential fail SILENTLY.

    Measured 2026-09-21: the missing-ADDRESS string fired the fail-fast gate,
    while the missing-CREDENTIAL string did not -- so a credential problem fell
    through to the blocking ``agy -p`` path for up to 1500s. The user saw the
    fast error only by luck of which string surfaced first.
    """

    def test_the_missing_credential_message_is_an_endpoint_failure(self):
        """DISCRIMINATING: this was False before the fix."""
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertTrue(is_endpoint_failure(
            "rpc error: code = Unauthenticated desc = missing CSRF token"))

    def test_the_invalid_credential_message_is_an_endpoint_failure(self):
        """DISCRIMINATING: the stale-token form; also False before the fix."""
        from swarmly.telegram.adapters.antigravity import is_endpoint_failure
        self.assertTrue(is_endpoint_failure(
            "rpc error: code = Unauthenticated desc = invalid CSRF token"))

    def test_the_credential_condition_is_recognised_by_the_helper(self):
        """DISCRIMINATING: mirrors the marker tuple so the two cannot drift."""
        self.assertTrue(endpoint.endpoint_error_is_credential(
            "rpc error: code = Unauthenticated desc = invalid CSRF token"))
        self.assertTrue(endpoint.endpoint_error_is_credential("missing csrf token"))
        self.assertFalse(endpoint.endpoint_error_is_credential("no active session"))
        self.assertFalse(endpoint.endpoint_error_is_credential(""))


if __name__ == "__main__":
    unittest.main()
