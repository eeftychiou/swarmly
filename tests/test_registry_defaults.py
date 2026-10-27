"""The registry flags are on by default on the store (phase 9b of #260, ``#424``).

A transport that runs the Telegram state store has every precondition the registry paths need,
so an omitted flag now defaults to the registry side there: ``state.lease``/``state.owed`` to
``store``, ``state.links``/``state.routing`` to ``registry``. With the store off — or with no
``state:`` section at all — every default is exactly what it was, so an existing config loads
unchanged and raises no new ``ConfigError``. An explicit ``shadow``/``legacy`` is still honoured
on the store too: downgrading is one config line per flag, not a backend rollback.

The defaults are read in ``AgentConfig.from_dict``, not from the ``StateSettings`` fields, so the
dataclass keeps stating the store-off default the docs table and §11.7 footnote both quote.
"""

import unittest

from swarmly.telegram.config import STATE_REGISTRY_DEFAULTS, ConfigError, StateSettings
from tests.test_telegram_channel import make_config

#: What an omitted flag is on the store, and what it still is with the store off.
ON_THE_STORE = {"lease": "store", "owed": "store", "links": "registry", "routing": "registry"}
WITH_THE_STORE_OFF = {"lease": "shadow", "owed": "shadow", "links": "legacy", "routing": "legacy"}


def state_flags(config) -> dict:
    """The four registry flags of a parsed config, as one comparable mapping."""
    return {name: getattr(config.state, name) for name in ON_THE_STORE}


class TheStoreDefaultsTests(unittest.TestCase):
    """``state.backend: telegram`` -> the registry side, with nothing else in the section."""

    def test_an_omitted_flag_is_the_registry_one_on_the_store(self):
        config = make_config(state={"backend": "telegram"})
        self.assertEqual(state_flags(config), ON_THE_STORE)

    def test_the_other_state_defaults_are_untouched(self):
        """#424 moves four flags. The mode, the cadence and the bus stay where they were."""
        config = make_config(state={"backend": "telegram"})
        self.assertEqual(config.state.backend, "telegram")
        self.assertEqual(config.state.mode, "shadow")
        self.assertEqual(config.state.heartbeat_minutes, 10.0)
        self.assertIsNone(config.state.dir)
        self.assertEqual(config.bus.transport, "git")      # gitbus off by default is 9c

    def test_a_second_key_in_the_section_does_not_disturb_the_defaults(self):
        config = make_config(state={"backend": "telegram", "heartbeat_minutes": 5,
                                    "dir": "/tmp/bot-state"})
        self.assertEqual(state_flags(config), ON_THE_STORE)
        self.assertEqual(config.state.heartbeat_minutes, 5.0)
        self.assertEqual(config.state.dir, "/tmp/bot-state")

    def test_writing_the_defaults_out_changes_nothing(self):
        """DISCRIMINATING: the omitted flags equal the values an explicit config would set.

        A default that only looked right in one place — the section present, or the flags
        spelled out — would pass one of these and fail the other.
        """
        omitted = make_config(state={"backend": "telegram"})
        spelled = make_config(state=dict(ON_THE_STORE, backend="telegram"))
        self.assertEqual(state_flags(omitted), state_flags(spelled))


class TheLegacyDefaultsTests(unittest.TestCase):
    """With the store off nothing moves: the defaults are the ones that shipped before #424."""

    def test_no_state_section_at_all_is_the_legacy_side(self):
        config = make_config()
        self.assertEqual(state_flags(config), WITH_THE_STORE_OFF)
        self.assertEqual(config.state.backend, "off")

    def test_an_explicit_off_backend_is_the_legacy_side(self):
        config = make_config(state={"backend": "off"})
        self.assertEqual(state_flags(config), WITH_THE_STORE_OFF)

    def test_an_empty_state_section_loads_where_it_used_to(self):
        """A section with only the cadence in it is a config that existed before #424."""
        config = make_config(state={"heartbeat_minutes": 15})
        self.assertEqual(state_flags(config), WITH_THE_STORE_OFF)
        self.assertEqual(config.state.heartbeat_minutes, 15.0)

    def test_the_legacy_defaults_are_the_dataclass_ones(self):
        """The dataclass fields stay the store-off statement the docs quote."""
        self.assertEqual({name: getattr(StateSettings(), name) for name in WITH_THE_STORE_OFF},
                         WITH_THE_STORE_OFF)
        self.assertEqual(sorted(STATE_REGISTRY_DEFAULTS), sorted(ON_THE_STORE))
        for name in ON_THE_STORE:
            with self.subTest(flag=name):
                self.assertNotEqual(STATE_REGISTRY_DEFAULTS[name], WITH_THE_STORE_OFF[name],
                                    "a store default that matches the legacy one is no default")


class TheExplicitOverrideTests(unittest.TestCase):
    """An explicit ``shadow`` / ``legacy`` is honoured on the store, one flag at a time."""

    def test_every_legacy_value_still_wins_on_the_store(self):
        config = make_config(state=dict(WITH_THE_STORE_OFF, backend="telegram"))
        self.assertEqual(state_flags(config), WITH_THE_STORE_OFF)

    def test_one_override_leaves_the_other_three_on_the_registry_side(self):
        config = make_config(state={"backend": "telegram", "routing": "legacy"})
        self.assertEqual(state_flags(config),
                         {"lease": "store", "owed": "store", "links": "registry",
                          "routing": "legacy"})

    def test_an_override_that_duplicates_the_default_is_not_an_error(self):
        """A config that spelled the new defaults out before #424 keeps loading."""
        config = make_config(state=dict(ON_THE_STORE, backend="telegram"))
        self.assertEqual(state_flags(config), ON_THE_STORE)

    def test_a_registry_value_still_needs_the_store(self):
        """Unchanged validation: the registry side is off the store only as a default, never
        as an explicit value."""
        for name, value in ON_THE_STORE.items():
            with self.subTest(flag=name):
                with self.assertRaises(ConfigError) as caught:
                    make_config(state={name: value})
                self.assertIn(f"state.{name}", str(caught.exception))

    def test_every_value_is_still_validated_on_the_store(self):
        for name in ("lease", "owed", "links", "routing"):
            with self.subTest(flag=name):
                with self.assertRaises(ConfigError) as caught:
                    make_config(state={"backend": "telegram", name: "halfway"})
                self.assertIn(f"state.{name}", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
