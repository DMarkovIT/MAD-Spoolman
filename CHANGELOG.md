# Changelog

## 1.0.1 — 2026-10-20

- Screenshots in the README: the Spoolman tab, the slicing-pipeline setting, and the result in
  the exported G-code.
- Plugin metadata now declares the plugin type, as required by the Orca Cloud publisher.
- Releases are published to Orca Cloud automatically from this repository.

## 1.0.0 — 2026-10-20

- First release.
- Spoolman tab with a spool picker for every filament preset.
- "Match spools to presets" creates presets for spools that have no match.
- The Spoolman G-code capability inserts the configured macro (default
  `SET_ACTIVE_SPOOL ID={id}`) after the filament start marker.
- English and Russian interface, follows the OrcaSlicer UI language.
