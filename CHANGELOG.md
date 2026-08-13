# Changelog

All notable changes to this project are listed in this file.

## 1.0.1 - 2026-08-13

- Add conditional configuration writes with SHA-256 stale-view checks.
- Add durable receipt states for interrupted install and removal recovery.
- Keep unrelated settings when a concurrent write can be detected.
- Add fixed error messages that do not expose configuration values.

## 1.0.0 - 2026-08-13

- Add a separate ordered fallback chain for each Hermes auxiliary model role.
- Use the configured Hermes provider and model catalog.
- Add default, named-profile, and all-profile installation modes.
- Add transactional install and removal with dated backups.
- Add a temporary-profile Vision fallback test.
- Add focused API and installer-helper tests.
