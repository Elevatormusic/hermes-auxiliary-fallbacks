"""Enable or disable the Auxiliary Fallbacks backend plugin."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PLUGIN_ID = "auxiliary-fallbacks"


def parse_args() -> argparse.Namespace:
    """Read command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("--hermes-agent", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Change only the Hermes plugin allow-list."""
    args = parse_args()
    agent_root = args.hermes_agent.resolve()
    if not (agent_root / "hermes_cli" / "config.py").is_file():
        raise SystemExit(f"Hermes Agent was not found at {agent_root}")

    sys.path.insert(0, str(agent_root))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(args.hermes_home.resolve())
    try:
        from hermes_cli.config import is_managed, load_config, save_config

        if is_managed():
            raise SystemExit(
                "This Hermes profile is managed. The installer cannot change its plugin allow-list."
            )

        config = load_config()
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            raise SystemExit("The plugins setting is not a mapping.")

        enabled = plugins.get("enabled", [])
        disabled = plugins.get("disabled", [])
        if not isinstance(enabled, list) or not isinstance(disabled, list):
            raise SystemExit("The plugin allow-list is not valid.")

        enabled_set = {str(item) for item in enabled}
        disabled_set = {str(item) for item in disabled}
        if args.action == "enable":
            enabled_set.add(PLUGIN_ID)
            disabled_set.discard(PLUGIN_ID)
        else:
            enabled_set.discard(PLUGIN_ID)
            disabled_set.add(PLUGIN_ID)

        plugins["enabled"] = sorted(enabled_set)
        plugins["disabled"] = sorted(disabled_set)
        save_config(config)

        saved_config = load_config()
        saved_plugins = saved_config.get("plugins", {})
        if not isinstance(saved_plugins, dict):
            raise SystemExit("Hermes did not save a valid plugin allow-list.")
        saved_enabled = saved_plugins.get("enabled", [])
        saved_disabled = saved_plugins.get("disabled", [])
        if not isinstance(saved_enabled, list) or not isinstance(saved_disabled, list):
            raise SystemExit("Hermes did not save a valid plugin allow-list.")
        enabled_after = {str(item) for item in saved_enabled}
        disabled_after = {str(item) for item in saved_disabled}
        if args.action == "enable":
            saved = PLUGIN_ID in enabled_after and PLUGIN_ID not in disabled_after
        else:
            saved = PLUGIN_ID not in enabled_after and PLUGIN_ID in disabled_after
        if not saved:
            raise SystemExit(
                f"Hermes did not persist the requested plugin state: {args.action}."
            )
    finally:
        reset_hermes_home_override(token)

    print(f"{PLUGIN_ID}: {args.action}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
