"""Verify the plugin-to-Hermes Vision fallback path with a temporary profile."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml


class QuotaHandler(BaseHTTPRequestHandler):
    """Return a deterministic quota error and record the request."""

    request_bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802
        """Reject the primary model request with HTTP 429."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).request_bodies.append(body)
        payload = json.dumps(
            {
                "error": {
                    "message": "You have reached your weekly usage limit.",
                    "type": "insufficient_quota",
                    "code": "usage_limit_reached",
                }
            }
        ).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep the test output concise."""


class CaptureHandler(logging.Handler):
    """Collect Hermes route log messages."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Save one formatted log message."""
        self.messages.append(record.getMessage())


def parse_args() -> argparse.Namespace:
    """Read command-line arguments."""
    default_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "hermes"
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--fallback-provider", required=True)
    parser.add_argument("--fallback-model", required=True)
    parser.add_argument("--hermes-root", type=Path, default=default_root)
    parser.add_argument("--hermes-agent", type=Path)
    parser.add_argument("--plugin-api", type=Path)
    parser.add_argument("--fallback-timeout", type=float, default=180.0)
    parser.add_argument(
        "--expect",
        action="append",
        required=True,
        help="Text that must occur in the structured image result. Repeat this option.",
    )
    return parser.parse_args()


def image_data_url(path: Path) -> str:
    """Build an image data URL for an OpenAI-compatible request."""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def response_text(response: object) -> str:
    """Read text from an OpenAI-compatible response."""
    message = response.choices[0].message  # type: ignore[attr-defined]
    return str(getattr(message, "content", "") or "").strip()


def snapshot(path: Path) -> bytes | None:
    """Read exact file bytes, or return None when the file is absent."""
    return path.read_bytes() if path.is_file() else None


def catalog_has_pair(state: dict[str, Any], provider: str, model: str) -> bool:
    """Return true when the plugin catalog contains a provider and model pair."""
    for row in state.get("catalog", {}).get("providers", []):
        if not isinstance(row, dict) or row.get("slug") != provider:
            continue
        for item in row.get("models", []):
            item_model = item.get("id") or item.get("name") if isinstance(item, dict) else item
            if str(item_model or "") == model:
                return True
    return False


def safe_provider_config(value: Any) -> Any:
    """Copy provider settings without credential values."""
    allowed_fields = {
        "api_mode",
        "base_url",
        "default_model",
        "display_name",
        "enabled",
        "featured_models",
        "key_env",
        "model",
        "models",
        "name",
    }
    secret_markers = ("api_key", "apikey", "authorization", "cookie", "credential", "password", "secret", "token")
    if isinstance(value, dict):
        return {
            str(key): safe_provider_config(item)
            for key, item in value.items()
            if str(key) in allowed_fields
            and not any(marker in str(key).lower().replace("-", "_") for marker in secret_markers)
        }
    if isinstance(value, list):
        return [safe_provider_config(item) for item in value]
    return value


def build_temp_config(root_config: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    """Build a small profile config for the selected configured route."""
    config: dict[str, Any] = {
        "_config_version": root_config.get("_config_version", 34),
        "model": {"provider": provider, "default": model},
        "auxiliary": {"vision": {"provider": "auto", "model": ""}},
    }
    for section in ("providers", "custom_providers"):
        rows = root_config.get(section)
        if isinstance(rows, dict) and provider in rows:
            config[section] = {provider: safe_provider_config(rows[provider])}
    return config


def load_plugin_api(path: Path) -> Any:
    """Load the plugin API from the selected install or workspace path."""
    spec = importlib.util.spec_from_file_location(
        f"auxiliary_fallback_smoke_api_{uuid.uuid4().hex}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The plugin API cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_structured_result(output: str) -> dict[str, Any]:
    """Parse and validate the exact Vision result fields."""
    text = output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"The Vision result is not JSON: {output}") from exc
    required = {"title", "first_role", "last_role", "role_count"}
    if not isinstance(result, dict) or set(result) != required:
        raise RuntimeError(f"The Vision result does not have the required fields: {output}")
    for key in ("title", "first_role", "last_role"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise RuntimeError(f"The Vision field '{key}' is empty: {output}")
    if isinstance(result["role_count"], bool) or not isinstance(result["role_count"], int) or result["role_count"] < 1:
        raise RuntimeError(f"The Vision role_count is not a positive integer: {output}")
    return result


def assert_safe_temp_profile(temp_dir: Path, profiles_root: Path, profile_name: str) -> None:
    """Reject cleanup unless the directory has the exact test-owned shape."""
    match = re.fullmatch(r"aux-fallback-smoke-([0-9a-f]{32})", profile_name)
    if match is None:
        raise RuntimeError(f"The temporary profile name is not safe: {profile_name}")
    uuid.UUID(hex=match.group(1))
    if temp_dir.is_symlink():
        raise RuntimeError(f"The temporary profile is a link: {temp_dir}")
    if temp_dir.resolve().parent != profiles_root.resolve():
        raise RuntimeError(f"The temporary profile is outside the profiles root: {temp_dir}")


def main() -> int:
    """Save a role chain through the plugin, then run a real Vision fallback."""
    args = parse_args()
    image = args.image.resolve()
    hermes_root = args.hermes_root.resolve()
    agent_root = (args.hermes_agent or hermes_root / "hermes-agent").resolve()
    repository_root = Path(__file__).resolve().parents[1]
    plugin_api_path = (
        args.plugin_api
        or repository_root
        / "plugin"
        / "agent"
        / "auxiliary-fallbacks"
        / "dashboard"
        / "plugin_api.py"
    ).resolve()
    if not image.is_file():
        raise SystemExit(f"Image was not found: {image}")
    if not (agent_root / "agent" / "auxiliary_client.py").is_file():
        raise SystemExit(f"Hermes Agent was not found: {agent_root}")
    if not plugin_api_path.is_file():
        raise SystemExit(f"The plugin API was not found: {plugin_api_path}")
    if not hermes_root.is_dir():
        raise SystemExit(f"The Hermes root was not found: {hermes_root}")

    root_config_path = hermes_root / "config.yaml"
    active_profile_path = hermes_root / "active_profile"
    active_name = ""
    if active_profile_path.is_file():
        active_name = active_profile_path.read_text(encoding="utf-8").strip()
    active_config_path = (
        hermes_root / "profiles" / active_name / "config.yaml"
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", active_name)
        and active_name.casefold() != "default"
        else root_config_path
    )
    protected_paths = {root_config_path, active_profile_path, active_config_path}
    protected_before = {path: snapshot(path) for path in protected_paths}
    active_config_raw = yaml.safe_load(active_config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(active_config_raw, dict):
        raise SystemExit(f"The active Hermes configuration is invalid: {active_config_path}")

    profiles_root = hermes_root / "profiles"
    profiles_root_existed = profiles_root.exists()
    profile_name = f"aux-fallback-smoke-{uuid.uuid4().hex}"
    temp_dir = profiles_root / profile_name
    temp_config_path = temp_dir / "config.yaml"
    assert_safe_temp_profile(temp_dir, profiles_root, profile_name)
    temp_dir.mkdir(parents=True, exist_ok=False)
    temp_config = build_temp_config(
        active_config_raw,
        args.fallback_provider,
        args.fallback_model,
    )
    temp_config_path.write_text(
        yaml.safe_dump(temp_config, sort_keys=False),
        encoding="utf-8",
    )

    old_home = os.environ.get("HERMES_HOME")
    inserted_path = str(agent_root)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, inserted_path)
    os.environ["HERMES_HOME"] = str(hermes_root)
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    profile_token: object | None = None
    logger: logging.Logger | None = None
    old_log_level: int | None = None
    capture = CaptureHandler()
    saved_temp_bytes: bytes | None = None
    result: dict[str, Any] | None = None
    failure: BaseException | None = None

    try:
        from agent import auxiliary_client
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        plugin_api = load_plugin_api(plugin_api_path)
        state = plugin_api.get_state(profile_name)
        if not state.get("compatible"):
            raise RuntimeError(state.get("compatibility_error") or "The plugin is not compatible.")
        if not catalog_has_pair(state, args.fallback_provider, args.fallback_model):
            raise RuntimeError(
                f"The temporary profile catalog does not contain "
                f"{args.fallback_provider}:{args.fallback_model}."
            )
        next_state = plugin_api.put_chain(
            "vision",
            {
                "revision": state["revision"],
                "chain": [
                    {
                        "provider": args.fallback_provider,
                        "model": args.fallback_model,
                    }
                ],
            },
            profile_name,
        )
        vision_state = next(
            task for task in next_state["tasks"] if task["key"] == "vision"
        )
        expected_pair = {
            "provider": args.fallback_provider,
            "model": args.fallback_model,
        }
        if vision_state.get("chain") != [expected_pair]:
            raise RuntimeError("The plugin API did not return the saved Vision chain.")
        saved_raw = yaml.safe_load(temp_config_path.read_text(encoding="utf-8")) or {}
        saved_chain = saved_raw.get("auxiliary", {}).get("vision", {}).get("fallback_chain")
        if saved_chain != [expected_pair]:
            raise RuntimeError("The plugin API did not save the exact Vision chain to disk.")
        saved_temp_bytes = temp_config_path.read_bytes()

        data_url = image_data_url(image)
        prompt = (
            "Analyze the attached screenshot. Return only one JSON object with exactly "
            "these keys: title, first_role, last_role, role_count. Use the main heading "
            "for title, the first visible auxiliary role for first_role, the last visible "
            "auxiliary role for last_role, and the number of visible role rows for role_count."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        QuotaHandler.request_bodies = []
        logger = logging.getLogger("agent.auxiliary_client")
        old_log_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(capture)
        profile_token = set_hermes_home_override(temp_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), QuotaHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        response = auxiliary_client.call_llm(
            task="vision",
            provider="custom",
            model="quota-primary",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="local-test-key",
            messages=messages,
            max_tokens=180,
            timeout=args.fallback_timeout,
        )
        result = parse_structured_result(response_text(response))

        route_label = f"fallback_chain[0]({args.fallback_provider})"
        if not any(route_label in message for message in capture.messages):
            raise RuntimeError("Hermes did not log the configured role fallback route.")
        if any(
            route_label in message and "stale/unrefreshable credential" in message
            for message in capture.messages
        ):
            raise RuntimeError("The configured role fallback had an unusable credential.")
        if not QuotaHandler.request_bodies:
            raise RuntimeError("The quota primary did not receive a request.")
        if not any(data_url.encode("utf-8") in body for body in QuotaHandler.request_bodies):
            raise RuntimeError("The quota primary did not receive the exact image data URL.")
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for expected in args.expect:
            if expected.casefold() not in result_text.casefold():
                raise RuntimeError(
                    f"The Vision result does not contain {expected!r}: {result_text}"
                )
    except BaseException as exc:
        failure = exc
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if logger is not None:
            logger.removeHandler(capture)
            if old_log_level is not None:
                logger.setLevel(old_log_level)
        if profile_token is not None:
            reset_hermes_home_override(profile_token)
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home
        if sys.path and sys.path[0] == inserted_path:
            sys.path.pop(0)
        sys.dont_write_bytecode = old_dont_write_bytecode

        protected_after = {path: snapshot(path) for path in protected_paths}
        drift_reasons: list[str] = []
        if protected_after != protected_before:
            drift_reasons.append("a protected active Hermes file changed")
        if saved_temp_bytes is not None and snapshot(temp_config_path) != saved_temp_bytes:
            drift_reasons.append("the temporary profile changed after the plugin save")
        if drift_reasons:
            print(f"PRESERVED temp_profile={temp_dir}")
            failure = RuntimeError("; ".join(drift_reasons))
        else:
            try:
                assert_safe_temp_profile(temp_dir, profiles_root, profile_name)
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                if not profiles_root_existed and profiles_root.is_dir() and not any(profiles_root.iterdir()):
                    profiles_root.rmdir()
            except BaseException as cleanup_exc:
                print(f"PRESERVED temp_profile={temp_dir}")
                failure = cleanup_exc

    if failure is not None:
        raise failure
    assert result is not None
    print(f"PASS api_chain={args.fallback_provider}:{args.fallback_model}")
    print(f"PASS route=fallback_chain[0]({args.fallback_provider})")
    print(f"PASS protected_files_unchanged={len(protected_paths)}")
    print(f"VISION_RESULT {json.dumps(result, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
