# Changelog

All notable changes to this project are listed in this file.

## 1.0.1 - 2026-08-13

- Add conditional configuration writes with SHA-256 stale-view checks.
- Add durable receipt states for interrupted install and removal recovery.
- Keep unrelated settings when a concurrent write can be detected.
- Add fixed error messages that do not expose configuration values.
- Reject redirected profile and configuration paths at request and write time.
- Preserve and verify configuration security metadata, owner identity, protected and unprotected DACL entries, DACL protection state, and source security state during replacement.
- Keep profile target paths lexical so safety checks can detect redirects.
- Read smoke-test provider settings from the active Hermes profile.
- Validate direct helper homes and configuration paths before an override or write.

## 1.0.0 - 2026-08-13

- Add a separate ordered fallback chain for each Hermes auxiliary model role.
- Use the configured Hermes provider and model catalog.
- Add default, named-profile, and all-profile installation modes.
- Add transactional install and removal with dated backups.
- Add a temporary-profile Vision fallback test.
- Add focused API and installer-helper tests.
