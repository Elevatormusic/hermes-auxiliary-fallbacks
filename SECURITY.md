# Security

## Report a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not put credentials, access tokens, private host names, or sensitive images in a public issue.

## Source boundary

This project must not change the Hermes Agent source tree or Electron application files. It installs two standalone plugin folders under a selected Hermes data home:

```text
<HERMES_HOME>/plugins/auxiliary-fallbacks/
<HERMES_HOME>/desktop-plugins/auxiliary-fallbacks/
```

The installer, removal script, and backend requests reject a Hermes home that is inside any detected Hermes Agent source directory. This check also covers a source checkout that differs from the selected Hermes runtime. They reject a symlink, junction, or other reparse-point ancestor that can redirect the selected home or `config.yaml`.

## Script effects

| File | Local writes | Network use |
|---|---|---|
| `scripts/install.ps1` | Copies the two plugin folders, updates the Hermes plugin allow-list, and makes dated backups. Rollback reverses only this plugin's allow-list membership. It keeps unrelated current settings. It moves failed copied folders to the backup instead of deleting them. | None, unless `-RestartGateway` starts a configured Hermes gateway that uses the network. |
| `scripts/uninstall.ps1` | Moves the two plugin folders to a dated backup and updates the plugin allow-list. It does not remove saved fallback chains. Rollback reverses only this plugin's allow-list membership and keeps unrelated current settings. | None, unless `-RestartGateway` starts a configured Hermes gateway that uses the network. |
| `scripts/plugin_state.py` | Changes only the Hermes plugin allow-list for the selected profile. It writes a transaction receipt and a persistent `.config.yaml.auxiliary-fallbacks.lock` sidecar. The sidecar serializes this extension's config transactions. Rollback rejects a conflicting membership. | None. |
| `plugin/agent/auxiliary-fallbacks/dashboard/plugin_api.py` and `config_write.py` | Change only one selected auxiliary fallback chain. They validate the profile and exact configuration path before and during the request. They use a SHA-256 stale-view guard, a plugin sidecar lock, and a same-directory atomic replacement. | None. |
| `scripts/profile_targets.py` | No writes. It resolves existing Hermes profile paths. | None. |
| `scripts/vision_fallback_smoke.py` | Creates one UUID-named temporary profile. It removes that profile after a clean test and preserves it if protected data changes. | Sends the selected image to the configured fallback provider. This action can use quota or incur cost. |

The sidecar lock coordinates only this extension. The conditional writer checks the exact file revision after it prepares the replacement and receipt, immediately before the atomic replace. It also checks the result. Changes that it can observe return a conflict and stay on disk.

Hermes Agent 0.20.0 does not provide a public cross-process compare-and-swap API or a lock that all Hermes configuration writers use. An unrelated process can still write or replace a path in the small interval between the final check and the filesystem system call. Do not save Hermes settings or change profile paths at the same time as a fallback-chain save, installation, or removal. The project does not change Hermes source or import its private locks to remove this platform limit.

## Credential handling

- The Desktop page receives a redacted provider catalog.
- The plugin does not create another credential store.
- The plugin uses provider setup that already exists in Hermes.
- The Vision smoke test does not copy provider credentials into its temporary profile. Registered OAuth providers can use the existing Hermes authentication flow.
- Do not use the live Vision test with a sensitive image.

## Update safety

Hermes updates can change plugin APIs. Version 1.0.1 is tested with a current Hermes Agent 0.20.0 build on Windows. The backend checks the public Hermes version and required configuration API before it writes a fallback chain. A future Hermes version can still need a plugin update if its public extension contract changes.
