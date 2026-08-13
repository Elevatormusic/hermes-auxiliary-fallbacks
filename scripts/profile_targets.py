"""Resolve Hermes profile names to profile home paths."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Read command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-agent", type=Path, required=True)
    parser.add_argument("--hermes-root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile")
    group.add_argument("--all-profiles", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Print selected profile homes as JSON."""
    args = parse_args()
    agent_root = args.hermes_agent.resolve()
    hermes_root = args.hermes_root.resolve()
    if not (agent_root / "hermes_cli" / "profiles.py").is_file():
        raise SystemExit(f"Hermes Agent was not found at {agent_root}")
    if not hermes_root.is_dir():
        raise SystemExit(f"The Hermes root was not found at {hermes_root}")

    sys.path.insert(0, str(agent_root))
    os.environ["HERMES_HOME"] = str(hermes_root)
    from hermes_cli.profiles import (  # noqa: PLC0415
        get_profile_dir,
        list_profiles,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )

    if args.all_profiles:
        rows = [
            {"name": info.name, "path": str(Path(info.path).resolve())}
            for info in list_profiles()
        ]
    else:
        try:
            name = normalize_profile_name(args.profile)
            validate_profile_name(name)
        except (TypeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        if not profile_exists(name):
            raise SystemExit(f"Hermes profile '{name}' does not exist.")
        rows = [{"name": name, "path": str(get_profile_dir(name).resolve())}]

    if not rows:
        raise SystemExit("Hermes did not return any profiles.")
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
