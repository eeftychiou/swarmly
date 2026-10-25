"""Behavioural tests for ``scripts/upgrade_transport.sh`` (PR #61, review F5).

The script's value is its FAILURE behaviour (auto-revert, dangling current,
unconfirmed or wrong-revision notices, re-switch, concurrency), so each
scenario runs the real script end-to-end against a small fixture clone of this
repository and fake ``systemctl``/``journalctl`` shims — no systemd required.

Environment: skipped when ``bash`` is unavailable (the script is bash-only).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.gitenv import git_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("UPGRADE_TRANSPORT_SCRIPT",
                             str(REPO_ROOT / "scripts" / "upgrade_transport.sh")))
BASH = shutil.which("bash")

FAKE_SYSTEMCTL = """\
#!/usr/bin/env bash
# Test shim for systemctl: state lives in $SHIM_FLAGS/active.
FLAGDIR="${SHIM_FLAGS:?}"
mkdir -p "$FLAGDIR"
case "${1:-}" in
  restart)
    shift
    if [ -e "$FLAGDIR/restart_fail_once" ]; then rm -f "$FLAGDIR/restart_fail_once"; exit 1; fi
    : > "$FLAGDIR/active"
    if [ -e "$FLAGDIR/is_active_fail_once" ]; then rm -f "$FLAGDIR/is_active_fail_once"; rm -f "$FLAGDIR/active"; fi
    exit 0 ;;
  is-active)
    if [ -e "$FLAGDIR/active" ]; then exit 0; else exit 3; fi ;;
  *) echo "fake-systemctl: unsupported: $*" >&2; exit 2 ;;
esac
"""

FAKE_JOURNALCTL = """#!/usr/bin/env bash
# Test shim for journalctl: prints the transport's REAL journal shape
# (swarmly/telegram/channel.py): journald prefix + logger name on every line; the
# connect line carries NO revision; the revision travels in the dedicated machine
# receipt (issue #70) and, for humans, in the HELLO notice that follows; the
# notice payload is repr()-quoted - Python picks the delimiter, so an
# apostrophe-bearing alias flips it to `"` (round-8 blocker). Neighbours include
# INBOUND text quoting the receipt itself and OUR OWN outbound reply quoting the
# notice (rounds 5-7 blockers): both are lines a selector keying on that text
# would wrongly accept.
# $JOURNAL_QUIET=1 -> silent journal; $JOURNAL_REV="none" -> machine receipt with
# NO rev= field; $JOURNAL_NO_RECEIPT=1 -> no receipt line at all;
# $JOURNAL_SPOOF_FIRST=1 -> the inbound receipt-quoting line comes BEFORE the
# receipt (an echo that arrives first must not win either);
# $JOURNAL_ALIAS overrides the alias (e.g. O'Brien for the delimiter case).
# $JOURNAL_CRLF=1 -> every record ends a CRLF pair (a Windows-shaped log
# consumer; #101 shape 2 - anchoring without CR-stripping false-reverts).
# $JOURNAL_QUOTE_TARGET=<sha> -> an ADDITIONAL outbound reply LAST, quoting the
# receipt at that revision (#101 shape 3, the masking class: pre-#101 this line
# outranked the genuine receipt under `tail -1`).
# $JOURNAL_INBOUND_BARE=1 -> the inbound echo drops its repr quotes and ends on
# the chat id, the shape the anchored selector would accept were the
# `inbound from` exclusion missing (#101 shape 4; synthetic).
if [ "${JOURNAL_QUIET:-0}" = "1" ]; then exit 0; fi
JRNL="Sep 20 20:23:33 host python3[1]: "
REV="${JOURNAL_REV:-abcdef0}"
ALIAS="${JOURNAL_ALIAS:-Fake}"
# CRLF knob: emit records the way a Windows-shaped log consumer delivers them.
# A naive end-anchor on `chat=-?[0-9]+$` never matches these (in GNU ERE `\\r`
# is a literal r); the script strips the CR BEFORE matching.
line() {
  if [ "${JOURNAL_CRLF:-0}" = "1" ]; then printf '%s\\r\\n' "$1"; else printf '%s\\n' "$1"; fi
}
# A peer relaying the receipt back to us. channel.py logs peer text verbatim, so
# this line is indistinguishable from our own receipt except for its prefix.
spoof() {
  if [ "${JOURNAL_INBOUND_BARE:-0}" = "1" ]; then
    line "${JRNL}swarmly.telegram: inbound from @peer (id=1, bot=True): quoting your log: A2A_ONLINE_NOTICE rev=deadbee alias=Peer chat=-100"
  else
    line "${JRNL}swarmly.telegram: inbound from @peer (id=1, bot=True): 'quoting your log: A2A_ONLINE_NOTICE rev=deadbee alias=Peer chat=-100'"
  fi
}
line "${JRNL}swarmly.telegram: connected as @fake-bot (alias ${ALIAS}) in chat -100"
if [ "${JOURNAL_SPOOF_FIRST:-0}" = "1" ]; then spoof; fi
if [ "${JOURNAL_NO_RECEIPT:-0}" != "1" ]; then
  if [ "$REV" = "none" ]; then
    # The two rev-less shapes, `rev=unknown` LAST: the selector takes `tail -1`,
    # so the line it actually sees is the real fail-closed value channel.py's
    # _git_head() emits when git is unreadable (synthetic; not read from a log).
    line "${JRNL}swarmly.telegram: A2A_ONLINE_NOTICE alias=${ALIAS} chat=-100"
    line "${JRNL}swarmly.telegram: A2A_ONLINE_NOTICE rev=unknown alias=${ALIAS} chat=-100"
  else
    line "${JRNL}swarmly.telegram: A2A_ONLINE_NOTICE rev=${REV} alias=${ALIAS} chat=-100"
  fi
fi
if [ "${JOURNAL_SPOOF_FIRST:-0}" != "1" ]; then spoof; fi
if [ "$REV" = "none" ]; then
  PAYLOAD="[A2A v1.1] HELLO to=* intent=ONLINE"
else
  PAYLOAD="[A2A v1.1] HELLO to=* intent=ONLINE rev=${REV}\\n\\n${ALIAS} online (adapter: fake, rev: ${REV})"
fi
# repr() compatibility: Python uses ' unless the string has ' and no \".
case "$PAYLOAD" in
  *\\"*) Q="'" ;;
  *\\'*) Q='"' ;;
  *) Q="'" ;;
esac
line "${JRNL}swarmly.telegram: outbound to chat -100: ${Q}${PAYLOAD}${Q}"
line "${JRNL}swarmly.telegram: outbound to chat -100: '[A2A v1.1] NOTE to=* intent=NOTE quoting the notice: outbound to chat -100: [A2A v1.1] HELLO to=* intent=ONLINE rev=deadbee'"
if [ -n "${JOURNAL_QUOTE_TARGET:-}" ]; then
  # #101 shape 3: our OWN later outbound reply quoting the receipt at the TARGET
  # revision - the masking line the pre-#101 `grep -F ... | tail -1` selector
  # picked and prefix-matched while the receipt still reported the old rev.
  line "${JRNL}swarmly.telegram: outbound to chat -100: '[A2A v1.1] NOTE to=* intent=NOTE quoting the receipt: A2A_ONLINE_NOTICE rev=${JOURNAL_QUOTE_TARGET} alias=${ALIAS} chat=-100'"
fi
exit 0
"""


@unittest.skipUnless(BASH and os.name == "posix",
                     "bash on POSIX paths required for the e2e fixtures — on "
                     "Windows run the suite inside WSL (git-bash/WSL bash cannot "
                     "drive the Windows-path fixture repo); see review note")
class UpgradeTransportTests(unittest.TestCase):
    """Each test drives the real script; assertions are on deploy-root state."""

    maxDiff = None

    # ------------------------------------------------------------------ setup
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upgrade-test-")
        base = Path(self.tmp)
        self.root = base / "deployroot"
        self.root.mkdir()
        self.flags = base / "flags"
        self.flags.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
            subprocess.run(["bash", "-n", str(p)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Fixture repo: clone of this repository with a FAST tests/ suite; the
        # script's verify gate runs the suite in a worktree at the target sha.
        self.repo = base / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks",
                        str(REPO_ROOT), str(self.repo)], check=True, env=git_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.repo / "tests")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_ok.py").write_text(
            "import unittest\n\n"
            "class Ok(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n")
        self.git("add", "-A")
        self.git("-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "fast tests")
        self.git("branch", "-f", "good")
        (self.repo / "tests" / "test_fail.py").write_text(
            "import unittest\n\n"
            "class Fail(unittest.TestCase):\n"
            "    def test_fail(self):\n"
            "        self.assertTrue(False)\n")
        self.git("add", "-A")
        self.git("-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "broken ref")
        self.git("branch", "-f", "broken")
        self.git("checkout", "-q", "--detach", "good")
        self.git("-c", "user.name=T", "-c", "user.email=t@t",
                 "commit", "-q", "--allow-empty", "-m", "good two")
        self.git("branch", "-f", "good2")
        self.git("checkout", "-q", "good")

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], env=git_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=git_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def sha(self, ref):
        out = subprocess.run(["git", "-C", str(self.repo), "rev-parse", ref],
                             capture_output=True, text=True, check=True, env=git_env())
        return out.stdout.strip()

    def deploy_of(self, ref):
        return str(self.root / ("deploy-" + self.sha(ref)))

    def build_env(self, env_extra=None, journal_rev="auto", ref=None):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "SHIM_FLAGS": str(self.flags),
            "UPGRADE_REPO": str(self.repo),
            "UPGRADE_DEPLOY_ROOT": str(self.root),
            "UPGRADE_PYTHON": sys.executable,
            "UPGRADE_SERVICE": "a2a-test",
            "UPGRADE_NOTICE_TIMEOUT": "2",
            "UPGRADE_SETTLE_SECONDS": "0",
            "UPGRADE_ROLLBACK_SETTLE_SECONDS": "0",
        }
        if journal_rev == "auto" and ref is not None:
            try:
                journal_rev = self.sha(ref)[:7]
            except subprocess.CalledProcessError:
                journal_rev = None
        if journal_rev:
            env["JOURNAL_REV"] = journal_rev
        if env_extra:
            env.update(env_extra)
        return env

    def run_script(self, *args, env_extra=None, journal_rev="auto", timeout=180,
                   extra_args=()):
        ref = None
        if len(args) >= 2 and args[0] == "switch":
            ref = args[1]
        env = self.build_env(env_extra, journal_rev, ref)
        return subprocess.run([BASH, str(SCRIPT), *args, *extra_args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def current_target(self):
        cur = self.root / "current"
        if not cur.is_symlink():
            return None
        resolved = os.path.realpath(str(cur))
        return resolved if os.path.exists(resolved) else "DANGLING"

    # ------------------------------------------------------------------ tests
    def test_broken_ref_refused_leaves_state_untouched(self):
        r = self.run_script("switch", "broken")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tests FAILED", r.stderr + r.stdout)
        self.assertIn("NOT switching", r.stderr + r.stdout)
        self.assertIsNone(self.current_target())

    def test_good_switch_then_rollback_roundtrip(self):
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))
        r2 = self.run_script("switch", "good2")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))
        self.assertEqual(os.path.realpath(str(self.root / "previous")),
                         self.deploy_of("good"))
        r3 = self.run_script("rollback")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))
        log = (self.root / "switch.log").read_text()
        self.assertIn("switch ", log)
        self.assertIn("rollback " + self.deploy_of("good"), log,
                      "the revert must be recorded too (round-3 minor)")

    def test_healthy_switch_with_production_two_line_journal(self):
        """PR #61 round-4 blocker: the transport emits the connect line with no
        revision and the revision on a separate line. A healthy switch with that
        real shape must succeed and must not auto-revert — the old fixture fused
        both fields into one line the transport never emits, so the suite
        green-lit a gate that reverts every healthy switch in production.
        Issue #70: the revision now travels in the dedicated machine receipt
        (`A2A_ONLINE_NOTICE rev=…`), not on the human notice; the connect line
        stays revision-free exactly as before."""
        r = self.run_script("switch", "good2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))
        self.assertNotIn("rolling back", r.stderr)
        self.assertNotIn("ROLLED BACK", r.stderr)

    def test_outbound_quoting_message_does_not_poison_gate(self):
        """Round-7 blocker (Antigravity): our OWN outbound line can quote the
        notice (channel.py logs outbound payloads verbatim). The shim emits
        exactly that hostile line after the receipt. Under #70 the winning line
        is the machine receipt by construction — a fixed literal from one call
        site — and the only residue is an echo of that literal, which the
        `grep -v 'inbound from'` exclusion covers."""
        r = self.run_script("switch", "good2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    # ------------------------------------------- gate-3 selector shapes (#101)
    def test_gate3_lf_receipt_passes(self):
        """#101 shape 1: a genuine LF-terminated machine receipt still passes
        the anchored selector — the baseline every healthy switch emits."""
        r = self.run_script("switch", "good2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))
        self.assertNotIn("ROLLED BACK", r.stderr)

    def test_gate3_crlf_receipt_passes(self):
        """#101 shape 2, the CRLF trap: a receipt whose record ends CRLF must
        PASS. In GNU ERE ``\\r`` is a literal `r`, so a naive fix that anchors on
        `chat=-?[0-9]+$` without `tr -d '\\r'` matches nothing and auto-reverts
        every healthy switch on such a host; this test is the pin against that
        regression."""
        r = self.run_script("switch", "good2", env_extra={"JOURNAL_CRLF": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))
        self.assertNotIn("ROLLED BACK", r.stderr)

    def test_gate3_later_outbound_quote_of_target_cannot_mask_old_rev(self):
        """#101 shape 3, the masking class this issue closes: the genuine
        receipt reports the OLD rev, and a LATER outbound line (our own reply,
        logged verbatim) quotes the TARGET sha in the receipt's shape. Pre-#101
        the bare `grep -F ... | tail -1` selector took the quoting line and
        prefix-matched the target — reporting success over a transport still on
        the old revision. Post-#101 the gate must FAIL and revert."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        old_rev = self.sha("good")[:7]
        r2 = self.run_script("switch", "good2", journal_rev=old_rev,
                             env_extra={"JOURNAL_QUOTE_TARGET": self.sha("good2")})
        self.assertNotEqual(r2.returncode, 0,
                            "a quoted target sha must not mask rev=" + old_rev)
        self.assertIn("rev=" + old_rev, r2.stderr)
        self.assertIn("target is", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_gate3_inbound_quote_alone_does_not_satisfy(self):
        """#101 shape 4: a window whose only receipt-shaped line is an inbound
        peer echo must NOT satisfy the gate. Emitted here in the quote-stripped
        form the anchored selector would otherwise accept (synthetic: the repr
        delimiters are gone, as on a host whose supervisor reformats records),
        so if the `grep -v 'inbound from'` exclusion were dropped the line
        would be selected and the revert would name a mismatched rev — the
        assertion below pins the receipt-ABSENT arm instead."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2",
                             env_extra={"JOURNAL_NO_RECEIPT": "1",
                                        "JOURNAL_INBOUND_BARE": "1"})
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no 'A2A_ONLINE_NOTICE rev=<sha>' receipt", r2.stderr)
        self.assertNotIn("notice reports", r2.stderr,
                         "the inbound echo must not be selected at all")
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_notice_with_apostrophe_alias_double_quote_delimiter(self):
        """Round-8 blocker (Antigravity): the notice is logged via %r, so an
        apostrophe-bearing alias flips repr's delimiter to `"`; a selector that
        hardcodes `: '` goes blind and false-reverts a healthy switch."""
        r = self.run_script("switch", "good2",
                            env_extra={"JOURNAL_ALIAS": "O'Brien"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_malformed_settle_seconds_fails_before_anything_mutates(self):
        """Review MINOR 3b: `UPGRADE_SETTLE_SECONDS=abc` used to abort at the
        sleep AFTER `current` was repointed (no rollback). It must fail loudly
        before anything mutates."""
        r = self.run_script("switch", "good2",
                            env_extra={"UPGRADE_SETTLE_SECONDS": "abc"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("UPGRADE_SETTLE_SECONDS", r.stderr)
        self.assertIsNone(self.current_target(), "nothing may be repointed")
        self.assertFalse((self.root / ".upgrade.lock").exists(),
                         "validation happens before the lock is taken")

    def test_malformed_lock_wait_names_the_real_cause(self):
        """Review MINOR 3a: `flock -w abc` made a malformed LOCK_WAIT look
        like a peer holding the lock."""
        r = self.run_script("switch", "good2",
                            env_extra={"UPGRADE_LOCK_WAIT": "abc"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("UPGRADE_LOCK_WAIT", r.stderr)
        self.assertNotIn("another upgrade_transport.sh invocation is active", r.stderr)
        self.assertIsNone(self.current_target())

    def test_malformed_notice_timeout_fails_before_repoint(self):
        """Review MINOR 3c: `UPGRADE_NOTICE_TIMEOUT=abc` died in the deadline
        arithmetic after the switch was committed."""
        r = self.run_script("switch", "good2",
                            env_extra={"UPGRADE_NOTICE_TIMEOUT": "abc"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("UPGRADE_NOTICE_TIMEOUT", r.stderr)
        self.assertIsNone(self.current_target())

    def test_inbound_rev_citing_message_does_not_poison_gate(self):
        """Rounds 5+6 blockers (Antigravity): the shim's `inbound from` line
        after the receipt quotes the receipt literal itself with a FOREIGN rev —
        the exact spoof r6 reproduced. channel.py logs peer message text
        verbatim, so the selector must drop `inbound from` lines (`grep -v`);
        without it the gate reverts a healthy switch."""
        r = self.run_script("switch", "good2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_restart_rc_failure_auto_reverts(self):
        """F1: a failing `systemctl restart` must roll back — the old script
        died under set -e with `current` left on the new (broken) revision."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        (self.flags / "restart_fail_once").write_text("")
        r2 = self.run_script("switch", "good2")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("rolling back automatically", r2.stderr)
        self.assertIn("ROLLED BACK", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"),
                         "current must be back on the previous deploy")
        self.assertTrue((self.flags / "active").exists(),
                        "the reverted service must be active")

    def test_is_active_failure_auto_reverts(self):
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        (self.flags / "is_active_fail_once").write_text("")
        r2 = self.run_script("switch", "good2")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("ROLLED BACK", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_silent_journal_auto_reverts(self):
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", env_extra={"JOURNAL_QUIET": "1"})
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no 'A2A_ONLINE_NOTICE rev=<sha>' receipt", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_wrong_rev_notice_auto_reverts(self):
        """F3: the notice's `rev:` receipt is verified against the target sha —
        a stale pre-restart line or a wrong revision must not pass the gate."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", journal_rev="deadbee")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("rev=deadbee", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_notice_without_rev_auto_reverts(self):
        """Round-3 finding 1: a receipt with NO rev field is silence, not
        evidence, so it must fail. `rev=unknown` (git unreadable) and a bare
        receipt are both synthetic forward-looking shapes, not reads from any
        real log.

        Not a pre-fix differential: the old selector's rev-less `connected as @`
        fallback reached this same "no rev field" arm, so the assertion held
        before the fix too (it asserted the old message, though). What #70
        changes is WHICH arm fires for a real transport: `rev=unknown` was
        previously filtered out of the receipt selection and misreported as
        absence."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", journal_rev="none")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no rev field", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_notice_without_rev_allow_unconfirmed(self):
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", journal_rev="none",
                             extra_args=("--allow-unconfirmed",))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("--allow-unconfirmed", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_window_with_hello_but_no_receipt_auto_reverts(self):
        """Issue #70 differential pin, and the pre-#70 transport shape: a window
        carrying the connect line and the outbound HELLO notice but no
        `A2A_ONLINE_NOTICE` receipt must NOT pass. A transport still at the
        pre-#70 revision logs exactly this, so the gate now needs
        `--allow-unconfirmed` to cut such a host over.

        That is deliberate, and not a rule-7 violation: UPGRADE.md rule 7 governs
        the WIRE (additive, ignore-unknown), and the receipt is additive there;
        gate 3 is a host-local cutover assertion expressing the TARGET contract.
        The pre-#70 selector matched this window's HELLO line and went green, so
        this test is red before the fix — unlike the two below."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", env_extra={"JOURNAL_NO_RECEIPT": "1"})
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no 'A2A_ONLINE_NOTICE rev=<sha>' receipt", r2.stderr)
        # Receipt ABSENT is a distinct arm from receipt PRESENT-but-rev-less; a
        # test asserting "no rev field" here would be pinning the wrong branch.
        self.assertNotIn("no rev field", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_allow_unconfirmed_accepts_a_window_with_no_receipt(self):
        """The documented escape hatch for the cutover above, and a differential:
        pre-#70 the old selector matched this window's HELLO unaided, so the
        switch passed silently and never printed the flag. Post-#70 the receipt
        is required, so the same switch only passes WITH `--allow-unconfirmed` —
        which is the difference this asserts."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2",
                             env_extra={"JOURNAL_NO_RECEIPT": "1"},
                             extra_args=("--allow-unconfirmed",))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("--allow-unconfirmed", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_receipt_echoed_before_the_receipt_does_not_poison_gate(self):
        """Order independence, not a pre-fix differential: peers quote log lines
        back at us (the swarm does this habitually), so the echo of the receipt
        may arrive BEFORE the receipt itself. `grep -v 'inbound from'` + `tail -1`
        is order-independent by construction; a selector that dropped the
        exclusion but used `head -1` would pass the trailing-echo case and fail
        this one. Same window as the poison test, only re-ordered."""
        r = self.run_script("switch", "good2", env_extra={"JOURNAL_SPOOF_FIRST": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_same_prefix_rev_rejected(self):
        """Round-3 finding 2: a revision sharing the target's 7-char prefix is
        NOT the target — the check is a prefix relation against the full sha."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        bogus = self.sha("good2")[:7] + "9" * 33
        r2 = self.run_script("switch", "good2", journal_rev=bogus)
        self.assertNotEqual(r2.returncode, 0,
                            "a same-prefix revision must not pass the gate")
        self.assertIn("target is", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_allow_unconfirmed_accepts_silent_notice(self):
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", env_extra={"JOURNAL_QUIET": "1"},
                             extra_args=("--allow-unconfirmed",))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("--allow-unconfirmed", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_skip_verify_marker_and_switch(self):
        """F7: a --skip-verify switch must say so in its success line."""
        r = self.run_script("switch", "broken", extra_args=("--skip-verify",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("UNVERIFIED — --skip-verify", r.stdout)
        self.assertEqual(self.current_target(), self.deploy_of("broken"))

    def test_dangling_current_switch_completes(self):
        """Round-2 F1: a dangling `current` must not abort the switch; with a
        usable previous recorded, the repair switch reports it as unchanged."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(self.root / "deploy-gone-away")
        r3 = self.run_script("switch", "good2")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertIn("previous left unchanged", r3.stderr,
                      "a usable previous exists; it must not be clobbered")
        self.assertEqual(self.current_target(), self.deploy_of("good2"))
        self.assertEqual(os.path.realpath(str(self.root / "previous")),
                         self.deploy_of("good"))

    def test_reswitch_keeps_previous(self):
        """F6: re-switching to the deployed revision must not alias previous to
        current (that would make rollback a no-op)."""
        for ref in ("good", "good2"):
            r = self.run_script("switch", ref)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_script("switch", "good2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("re-switching to the deployed revision", r.stderr)
        self.assertEqual(os.path.realpath(str(self.root / "previous")),
                         self.deploy_of("good"))
        r2 = self.run_script("rollback")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_switch_log_written(self):
        """F8: a durable pre-switch record exists for hand recovery."""
        r = self.run_script("switch", "good")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = (self.root / "switch.log").read_text()
        self.assertIn("switch " + self.sha("good"), log)

    # ------------------------------------------------- service lifecycle (#84)
    def lifecycle_env(self, service="a2a-test", receipt=None, start=None,
                      is_active=None, since_file=None):
        """Env for a host that owns the service lifecycle (issue #84).

        Mirrors the Task-Scheduler deployment in docs/telegram/UPGRADE.md: the
        snippets are the supervisor and the unit name is only a label.
        """
        env = {
            "UPGRADE_SERVICE": service,
            "UPGRADE_SERVICE_START": start if start is not None
            else "systemctl restart a2a-test",
            "UPGRADE_SERVICE_IS_ACTIVE": is_active if is_active is not None
            else "systemctl is-active --quiet a2a-test",
            "UPGRADE_SERVICE_RECEIPT": receipt if receipt is not None
            else 'journalctl -u a2a-test --since "@$since" --no-pager',
        }
        if since_file is not None:
            # Proves the hook is called with the restart's epoch in $since.
            env["UPGRADE_SERVICE_RECEIPT"] = (
                'printf "%s" "$since" > "{0}"\n'
                'journalctl -u a2a-test --since "@$since" --no-pager'
            ).format(since_file)
        return env

    def test_lifecycle_hooks_allow_omitting_the_service_name(self):
        """#84: with all three hooks set, no unit name is needed — so the
        switch/auto-revert drill runs on a host with no systemd at all."""
        since = self.flags / "receipt_since"
        r = self.run_script("switch", "good",
                            env_extra=self.lifecycle_env(service="", since_file=since))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))
        self.assertTrue(since.exists(), "the receipt hook was never called")
        passed = float(since.read_text())
        self.assertLessEqual(abs(time.time() - passed), 300,
                             "the receipt hook must receive the restart's epoch")

    def test_partial_lifecycle_hooks_still_require_a_service_name(self):
        """A partial set must not silently fall back to systemd for the rest."""
        r = self.run_script("switch", "good", env_extra={
            "UPGRADE_SERVICE": "",
            "UPGRADE_SERVICE_START": "systemctl restart a2a-test",
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no service name", r.stderr)
        self.assertIsNone(self.current_target())

    def test_failing_lifecycle_start_hook_auto_reverts(self):
        """Gate 2 must go through the seam: a custom start hook's non-zero exit
        takes the same auto-revert path as a failing `systemctl restart`."""
        calls = self.flags / "start_calls"
        start = ('n=$(cat "{0}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{0}"\n'
                 'if [ "$n" -eq 2 ]; then echo "hook: refusing to start" >&2; exit 1; fi\n'
                 'systemctl restart a2a-test').format(calls)
        r1 = self.run_script("switch", "good", env_extra=self.lifecycle_env(start=start))
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", env_extra=self.lifecycle_env(start=start))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("rolling back automatically", r2.stderr)
        self.assertIn("ROLLED BACK", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"),
                         "the reverted deploy must be the previous one")
        self.assertTrue((self.flags / "active").exists(),
                        "the rollback restarted the supervisor through the hook")

    def test_receipt_hook_is_the_notice_source(self):
        """Gate 3 must read the receipt through the receipt hook: a hook that
        prints nothing fails the gate rather than silently passing."""
        r1 = self.run_script("switch", "good", env_extra=self.lifecycle_env())
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2",
                             env_extra=self.lifecycle_env(receipt="true"))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no 'A2A_ONLINE_NOTICE rev=<sha>' receipt", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_receipt_hook_cannot_bypass_the_rev_check(self):
        """A host-supplied receipt narrows WHERE the gate reads, not WHAT it
        accepts: a mismatched revision still auto-reverts."""
        r1 = self.run_script("switch", "good", env_extra=self.lifecycle_env())
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_script("switch", "good2", journal_rev="deadbee",
                             env_extra=self.lifecycle_env())
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("target is", r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good"))

    def test_status_names_a_non_portable_configuration(self):
        """#84: `status` used to print the default repo path and exit 0 on any
        host, including ones where that path does not exist."""
        r = self.run_script("status",
                            env_extra={"UPGRADE_REPO": str(Path(self.tmp) / "nowhere"),
                                       "UPGRADE_DEPLOY_ROOT": str(Path(self.tmp) / "noroot"),
                                       "UPGRADE_SERVICE": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not a git checkout", r.stderr)
        self.assertIn("deploy root does not exist", r.stderr)
        self.assertIn("set UPGRADE_SERVICE", r.stdout)

    @unittest.skipUnless(shutil.which("flock"), "flock required")
    def test_lock_held_by_peer_refuses(self):
        """NIT 4: prove mutual exclusion itself — hold the lock externally and
        the script must refuse (not merely race-favourably)."""
        lock_path = self.root / ".upgrade.lock"
        holder = subprocess.Popen(
            ["flock", "-x", str(lock_path), "-c", "sleep 5"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                probe = subprocess.run(["flock", "-n", str(lock_path), "-c", "true"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode != 0:
                    break
                time.sleep(0.1)
            r = self.run_script("switch", "good2",
                                env_extra={"UPGRADE_LOCK_WAIT": "1"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("another upgrade_transport.sh invocation is active", r.stderr)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        r2 = self.run_script("switch", "good2")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))

    def test_concurrent_switches_serialise(self):
        """F3 (round 2): the flock must serialise two live switches — no
        interleaved worktree/verify corruption."""
        r1 = self.run_script("switch", "good")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        env = self.build_env(ref="good2")
        p1 = subprocess.Popen([BASH, str(SCRIPT), "switch", "good2"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([BASH, str(SCRIPT), "switch", "good2"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out1 = p1.communicate(timeout=180)
        out2 = p2.communicate(timeout=180)
        self.assertEqual(p1.returncode, 0, out1)
        self.assertEqual(p2.returncode, 0, out2)
        self.assertEqual(self.current_target(), self.deploy_of("good2"))


if __name__ == "__main__":
    unittest.main()
