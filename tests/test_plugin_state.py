"""Test safe plugin allow-list changes without a Hermes profile."""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from unittest import TestCase, main, mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plugin_state.py"


def load_script():
    """Load a separate script module for one test."""
    name = f"plugin_state_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginStateTests(TestCase):
    """Check managed-mode rejection and post-save verification."""

    def setUp(self) -> None:
        self.fixture = Path(__file__).parent / f"state-fixture-{uuid.uuid4().hex}"
        (self.fixture / "hermes_cli").mkdir(parents=True)
        (self.fixture / "hermes_cli" / "config.py").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        for path in sorted(self.fixture.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        self.fixture.rmdir()

    def modules(self, *, managed: bool, persist: bool):
        """Create a small Hermes configuration runtime."""
        state = {
            "config": {"plugins": {"enabled": [], "disabled": []}},
            "save_calls": 0,
        }

        def load_config():
            return copy.deepcopy(state["config"])

        def save_config(config):
            state["save_calls"] += 1
            if persist:
                state["config"] = copy.deepcopy(config)

        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        config = types.ModuleType("hermes_cli.config")
        config.is_managed = lambda: managed
        config.load_config = load_config
        config.save_config = save_config
        constants = types.ModuleType("hermes_constants")
        constants.set_hermes_home_override = lambda _path: object()
        constants.reset_hermes_home_override = lambda _token: None
        return state, {
            "hermes_cli": package,
            "hermes_cli.config": config,
            "hermes_constants": constants,
        }

    def run_action(self, action: str, modules: dict[str, types.ModuleType]) -> int:
        """Run one helper action with the test runtime."""
        script = load_script()
        argv = [
            "plugin_state.py",
            action,
            "--hermes-agent",
            str(self.fixture),
            "--hermes-home",
            str(self.fixture / "home"),
        ]
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "argv", argv):
            return script.main()

    def test_managed_profile_is_rejected_before_save(self) -> None:
        state, modules = self.modules(managed=True, persist=True)
        with self.assertRaisesRegex(SystemExit, "profile is managed"):
            self.run_action("enable", modules)
        self.assertEqual(state["save_calls"], 0)

    def test_silent_save_is_rejected(self) -> None:
        state, modules = self.modules(managed=False, persist=False)
        with self.assertRaisesRegex(SystemExit, "did not persist"):
            self.run_action("enable", modules)
        self.assertEqual(state["save_calls"], 1)

    def test_enable_is_verified_after_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        self.assertEqual(self.run_action("enable", modules), 0)
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])

    def test_disable_is_verified_after_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        state["config"]["plugins"]["enabled"] = ["auxiliary-fallbacks"]
        self.assertEqual(self.run_action("disable", modules), 0)
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])


if __name__ == "__main__":
    main()
