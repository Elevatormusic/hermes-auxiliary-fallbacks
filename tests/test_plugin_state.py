"""Test safe plugin allow-list changes without a Hermes profile."""

from __future__ import annotations

import copy
import hashlib
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
            "read_calls": 0,
            "save_calls": 0,
        }

        def load_config(_path=None):
            state["read_calls"] += 1
            hook = state.get("on_read")
            if hook is not None:
                hook(state)
            return copy.deepcopy(state["config"])

        class ConfigConflict(RuntimeError):
            """Represent one conditional write conflict."""

        def revision(path):
            try:
                content = Path(path).read_bytes()
            except FileNotFoundError:
                return "sha256:missing"
            return f"sha256:{hashlib.sha256(content).hexdigest()}"

        def conditional_writer(
            path,
            key_path,
            value,
            expected_revision,
            *,
            before_replace=None,
            after_replace=None,
        ):
            path = Path(path)
            marker = f"saved-{state['save_calls'] + 1}"
            candidate = f"sha256:{hashlib.sha256(marker.encode()).hexdigest()}"
            if before_replace is not None:
                before_replace(candidate)
            hook = state.get("after_receipt")
            if hook is not None:
                hook(state, path)
            if revision(path) != expected_revision:
                raise ConfigConflict("revision changed")
            state["save_calls"] += 1
            state["save_options"] = {"key_path": key_path}
            if persist:
                state["config"][key_path] = copy.deepcopy(value)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker, encoding="utf-8")
            if after_replace is not None:
                after_replace(candidate)
            hook = state.get("after_replace")
            if hook is not None:
                hook(state, path)
            if revision(path) != candidate:
                raise ConfigConflict("revision changed after replace")
            return candidate

        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        config = types.ModuleType("hermes_cli.config")
        config.is_managed = lambda: managed
        config.get_config_path = lambda: self.fixture / "home" / "config.yaml"
        config.read_raw_config = lambda: (_ for _ in ()).throw(
            AssertionError("The cached raw reader must not be used.")
        )
        config.read_user_config_raw = load_config
        utils = types.ModuleType("utils")
        utils.conditional_writer = conditional_writer
        utils.ConfigConflict = ConfigConflict
        constants = types.ModuleType("hermes_constants")
        constants.set_hermes_home_override = lambda _path: object()
        constants.reset_hermes_home_override = lambda _token: None
        return state, {
            "hermes_cli": package,
            "hermes_cli.config": config,
            "hermes_constants": constants,
            "utils": utils,
        }

    def run_action(
        self,
        action: str,
        modules: dict[str, types.ModuleType],
        receipt: Path | None = None,
        configure=None,
    ) -> int:
        """Run one helper action with the test runtime."""
        script = load_script()
        utils = modules["utils"]
        script._load_config_write_support = lambda: (
            utils.conditional_writer,
            utils.ConfigConflict,
        )
        if configure is not None:
            configure(script)
        argv = [
            "plugin_state.py",
            action,
            "--hermes-agent",
            str(self.fixture),
            "--hermes-home",
            str(self.fixture / "home"),
            "--receipt",
            str(receipt or (self.fixture / f"{action}.receipt.json")),
        ]
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "argv", argv):
            return script.main()

    def test_managed_profile_is_rejected_before_save(self) -> None:
        state, modules = self.modules(managed=True, persist=True)
        with self.assertRaisesRegex(SystemExit, "profile is managed"):
            self.run_action("enable", modules)
        self.assertEqual(state["save_calls"], 0)

    def test_direct_helper_rejects_source_and_redirected_homes(self) -> None:
        script = load_script()
        source_home = self.fixture / "data" / "profile"
        (self.fixture / "hermes_constants.py").write_text("", encoding="utf-8")
        (self.fixture / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "inside a Hermes Agent source directory"):
            script._validate_hermes_home(source_home)

        safe_home = self.fixture.parent / f"home-{uuid.uuid4().hex}"
        script._is_reparse_point = lambda candidate: candidate == safe_home
        with self.assertRaisesRegex(SystemExit, "uses a redirected path"):
            script._validate_hermes_home(safe_home)

    def test_main_rejects_an_unsafe_home_before_override_or_write(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        calls: list[Path] = []
        modules["hermes_constants"].set_hermes_home_override = lambda path: calls.append(path)
        (self.fixture / "hermes_constants.py").write_text("", encoding="utf-8")
        (self.fixture / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "inside a Hermes Agent source directory"):
            self.run_action("enable", modules)

        self.assertEqual(calls, [])
        self.assertEqual(state["save_calls"], 0)

    def test_silent_save_is_rejected(self) -> None:
        state, modules = self.modules(managed=False, persist=False)
        receipt = self.fixture / "silent.receipt.json"
        with self.assertRaisesRegex(SystemExit, "did not persist"):
            self.run_action("enable", modules, receipt)
        self.assertEqual(state["save_calls"], 1)
        self.assertTrue(receipt.is_file())

    def test_enable_is_verified_after_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        self.assertEqual(self.run_action("enable", modules), 0)
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])
        self.assertEqual(state["save_options"]["key_path"], "plugins")

    def test_disable_is_verified_after_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        state["config"]["plugins"]["enabled"] = ["auxiliary-fallbacks"]
        self.assertEqual(self.run_action("disable", modules), 0)
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])

    def test_rollback_preserves_unrelated_current_settings(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "rollback.receipt.json"
        self.assertEqual(self.run_action("enable", modules, receipt), 0)

        state["config"]["theme"] = "current-user-value"
        state["config"]["plugins"]["enabled"].append("another-plugin")
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)

        self.assertEqual(state["config"]["theme"], "current-user-value")
        self.assertEqual(state["config"]["plugins"]["enabled"], ["another-plugin"])
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])

    def test_rollback_rejects_target_membership_drift(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "drift.receipt.json"
        self.assertEqual(self.run_action("enable", modules, receipt), 0)
        state["config"]["plugins"]["disabled"].append("auxiliary-fallbacks")
        save_calls = state["save_calls"]

        with self.assertRaisesRegex(SystemExit, "changed after this transaction"):
            self.run_action("rollback", modules, receipt)

        self.assertEqual(state["save_calls"], save_calls)
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["disabled"])

    def test_prepared_receipt_does_not_undo_matching_external_change(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "prepared.receipt.json"
        script = load_script()
        script._write_receipt(
            receipt,
            action="enable",
            before={"enabled": False, "disabled": False},
            after={"enabled": True, "disabled": False},
        )
        state["config"]["plugins"]["enabled"].append("auxiliary-fallbacks")

        self.assertEqual(self.run_action("rollback", modules, receipt), 0)

        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertEqual(state["save_calls"], 0)

    def test_apply_rejects_config_change_before_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        config_path = self.fixture / "home" / "config.yaml"
        config_path.parent.mkdir(parents=True)

        def change_on_second_read(runtime_state) -> None:
            if runtime_state["read_calls"] == 2:
                config_path.write_text("theme: concurrent\n", encoding="utf-8")
                runtime_state["config"]["theme"] = "concurrent"

        state["on_read"] = change_on_second_read
        receipt = self.fixture / "concurrent.receipt.json"
        with self.assertRaisesRegex(SystemExit, "configuration changed"):
            self.run_action("enable", modules, receipt)

        self.assertEqual(state["save_calls"], 0)
        self.assertEqual(state["config"]["theme"], "concurrent")
        self.assertTrue(receipt.is_file())
        self.assertEqual(
            load_script()._read_receipt(receipt)["status"],
            "prepared",
        )

    def test_matching_external_change_keeps_prepared_receipt(self) -> None:
        state, modules = self.modules(managed=False, persist=True)

        def enable_on_second_read(runtime_state) -> None:
            if runtime_state["read_calls"] == 2:
                runtime_state["config"]["plugins"]["enabled"].append(
                    "auxiliary-fallbacks"
                )

        state["on_read"] = enable_on_second_read
        receipt = self.fixture / "external.receipt.json"
        with self.assertRaisesRegex(SystemExit, "allow-list changed"):
            self.run_action("enable", modules, receipt)

        self.assertEqual(load_script()._read_receipt(receipt)["status"], "prepared")
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)
        self.assertIn(
            "auxiliary-fallbacks",
            state["config"]["plugins"]["enabled"],
        )
        self.assertEqual(state["save_calls"], 0)

    def test_change_after_receipt_arm_is_not_overwritten(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        config_path = self.fixture / "home" / "config.yaml"
        config_path.parent.mkdir(parents=True)

        def external_write(runtime_state, path) -> None:
            runtime_state["after_receipt"] = None
            runtime_state["config"]["plugins"]["custom_flag"] = "external"
            path.write_text("plugins:\n  custom_flag: external\n", encoding="utf-8")

        state["after_receipt"] = external_write
        receipt = self.fixture / "after-arm.receipt.json"
        with self.assertRaisesRegex(SystemExit, "configuration changed"):
            self.run_action("enable", modules, receipt)

        self.assertEqual(state["save_calls"], 0)
        self.assertEqual(state["config"]["plugins"]["custom_flag"], "external")
        self.assertEqual(load_script()._read_receipt(receipt)["status"], "armed")
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)
        self.assertEqual(state["config"]["plugins"]["custom_flag"], "external")

    def test_armed_receipt_rejects_a_non_candidate_revision(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        config_path = self.fixture / "home" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("before", encoding="utf-8")
        receipt = self.fixture / "armed-drift.receipt.json"
        script = load_script()
        script._write_receipt(
            receipt,
            action="enable",
            before={"enabled": False, "disabled": False},
            after={"enabled": True, "disabled": False},
        )
        script._arm_receipt(receipt, "sha256:" + "0" * 64)
        state["config"]["plugins"]["enabled"].append("auxiliary-fallbacks")
        config_path.write_text("external", encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "changed after this transaction"):
            self.run_action("rollback", modules, receipt)

        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertEqual(state["save_calls"], 0)

    def test_receipt_arm_failure_happens_before_save(self) -> None:
        state, modules = self.modules(managed=False, persist=True)

        def fail_receipt(_path, _candidate) -> None:
            raise OSError("receipt replace failed")

        with self.assertRaisesRegex(SystemExit, "cannot save the plugin allow-list"):
            self.run_action(
                "enable",
                modules,
                self.fixture / "finalization.receipt.json",
                configure=lambda script: setattr(
                    script,
                    "_arm_receipt",
                    fail_receipt,
                ),
            )

        self.assertEqual(state["save_calls"], 0)
        self.assertNotIn(
            "auxiliary-fallbacks",
            state["config"]["plugins"]["enabled"],
        )

    def test_writer_error_does_not_expose_configuration_values(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "safe-error.receipt.json"

        def fail_with_secret(*_args, **_kwargs) -> None:
            raise RuntimeError("api_key: must-not-leak")

        modules["utils"].conditional_writer = fail_with_secret
        with self.assertRaisesRegex(
            SystemExit,
            "Hermes cannot save the plugin allow-list",
        ) as caught:
            self.run_action("enable", modules, receipt)

        self.assertNotIn("must-not-leak", str(caught.exception))
        self.assertEqual(load_script()._read_receipt(receipt)["status"], "prepared")
        self.assertEqual(state["save_calls"], 0)

    def test_receipt_apply_failure_is_recoverable(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "apply-failure.receipt.json"

        def fail_receipt(_path) -> None:
            raise OSError("receipt apply failed")

        with self.assertRaisesRegex(OSError, "receipt apply failed"):
            self.run_action(
                "enable",
                modules,
                receipt,
                configure=lambda script: setattr(
                    script,
                    "_mark_receipt_applied",
                    fail_receipt,
                ),
            )

        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertEqual(load_script()._read_receipt(receipt)["status"], "committed")
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])

    def test_sibling_change_after_replace_is_preserved_by_rollback(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        receipt = self.fixture / "after-replace.receipt.json"

        def external_write(runtime_state, path) -> None:
            runtime_state["after_replace"] = None
            runtime_state["config"]["plugins"]["custom_flag"] = "external"
            path.write_text("saved-by-external-writer", encoding="utf-8")

        state["after_replace"] = external_write
        with self.assertRaisesRegex(SystemExit, "configuration changed"):
            self.run_action("enable", modules, receipt)

        self.assertEqual(load_script()._read_receipt(receipt)["status"], "committed")
        self.assertIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)
        self.assertNotIn("auxiliary-fallbacks", state["config"]["plugins"]["enabled"])
        self.assertEqual(state["config"]["plugins"]["custom_flag"], "external")

    def test_rollback_recovers_when_save_raises_after_write(self) -> None:
        state, modules = self.modules(managed=False, persist=True)
        utils_module = modules["utils"]
        original_save = utils_module.conditional_writer
        receipt = self.fixture / "write-then-fail.receipt.json"

        def write_then_fail(*args, **kwargs) -> None:
            original_save(*args, **kwargs)
            raise OSError("failure after write")

        utils_module.conditional_writer = write_then_fail
        with self.assertRaisesRegex(SystemExit, "cannot save the plugin allow-list"):
            self.run_action("enable", modules, receipt)

        self.assertIn(
            "auxiliary-fallbacks",
            state["config"]["plugins"]["enabled"],
        )
        self.assertEqual(load_script()._read_receipt(receipt)["status"], "committed")

        utils_module.conditional_writer = original_save
        self.assertEqual(self.run_action("rollback", modules, receipt), 0)
        self.assertNotIn(
            "auxiliary-fallbacks",
            state["config"]["plugins"]["enabled"],
        )


if __name__ == "__main__":
    main()
