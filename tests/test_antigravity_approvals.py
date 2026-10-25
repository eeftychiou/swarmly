"""Review P0-5: Antigravity skips approvals only when its configured command says so."""

import tempfile
import unittest

from swarmly.telegram.adapters import antigravity
from swarmly.telegram.adapters.antigravity import AntigravityAdapter, AntigravityThreadAdapter
from swarmly.telegram.config import AdapterSettings

FLAG = "--dangerously-skip-permissions"
CONV = "12345678-1234-5678-1234-567812345678"


class TestExplicitCommandIsRespected(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name

    def test_a_command_without_the_flag_keeps_approvals(self):
        for command in ("agy -p {prompt} --print-timeout 20m",
                        f"agy -p {{prompt}} --conversation {CONV}"):
            with self.subTest(command=command):
                settings = AdapterSettings(type="antigravity", cwd=self.cwd, command=command)
                self.assertNotIn(FLAG, AntigravityAdapter("Antigravity", settings)
                                 .build_argv("hello"))

    def test_the_default_command_still_skips_them(self):
        # D4: the default level is unchanged; only the hidden append is gone.
        settings = AdapterSettings(type="antigravity", cwd=self.cwd, conversation=CONV)
        self.assertIn(FLAG, AntigravityAdapter("Antigravity", settings).build_argv("hello"))
        self.assertTrue(antigravity.skips_approvals(AdapterSettings(type="antigravity")))

    def test_the_thread_adapter_follows_the_command_too(self):
        plain = AdapterSettings(type="antigravity_thread", cwd=self.cwd,
                                command="agy -p {prompt}")
        default = AdapterSettings(type="antigravity_thread", cwd=self.cwd)
        self.assertNotIn(FLAG, AntigravityThreadAdapter("Antigravity", plain)
                         .build_fallback_argv("hi", "auto"))
        self.assertIn(FLAG, AntigravityThreadAdapter("Antigravity", default)
                      .build_fallback_argv("hi", "auto"))

    def test_the_flag_must_be_a_whole_argument(self):
        command = "agy -p {prompt} --title no" + FLAG
        settings = AdapterSettings(type="antigravity", command=command)
        self.assertFalse(antigravity.skips_approvals(settings))

    def test_the_thread_fallback_keeps_the_sandbox(self):
        settings = AdapterSettings(type="antigravity_thread", cwd=self.cwd,
                                   command="agy -p {prompt} --sandbox")
        argv = AntigravityThreadAdapter("Antigravity", settings).build_fallback_argv("hi", "auto")
        self.assertIn("--sandbox", argv)
        self.assertNotIn(FLAG, argv)

    def test_a_quoted_flag_is_read_as_the_argv_builder_reads_it(self):
        settings = AdapterSettings(type="antigravity",
                                   command=f"agy -p {{prompt}} '{FLAG}'")
        self.assertTrue(antigravity.skips_approvals(settings))

    def test_windows_splits_the_way_the_windows_argv_builder_does(self):
        from unittest import mock
        command = rf'C:\agy\agy.exe -p {{prompt}} "{FLAG}"'
        settings = AdapterSettings(type="antigravity", command=command)
        with mock.patch.object(antigravity.os, "name", "nt"):
            words = antigravity._command_words(settings)
            self.assertTrue(antigravity.skips_approvals(settings))
        self.assertEqual(words[0], r"C:\agy\agy.exe")         # POSIX mode eats the "\"


if __name__ == "__main__":
    unittest.main()
