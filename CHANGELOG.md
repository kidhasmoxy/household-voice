# Changelog

## 0.2.2

Supports Home Assistant 2026.9.4 without requiring a core upgrade. Both the
validation and release workflows test the integration lifecycle against
2026.9.4 and 2026.10.1 before publishing an installation asset. Optional LLM tool
annotations are attached only on HA versions that expose that interface. Tool
results follow each version's native contract: JSON mappings on 2026.9.4 and
ToolResult objects on 2026.10.1. The actual-HA fixture constructs all 17 tools
and checks safe calls through all four tool APIs on both versions.

## 0.2.1

Public prerelease: one Home Assistant custom integration with an embedded
durable coordinator, shared schedules, management UI, Music Assistant transports,
recorded broadcasts, configurable room mappings, and optional research tools.

Fixes cross-platform release hashing by normalizing source text to LF and
declaring Git line-ending attributes. The v0.2.0 release workflow correctly
rejected inconsistent hashes; it produced no installation release.

Physical household acceptance and firmware provisioning are installation steps;
automated packaging checks do not establish acoustic or device compatibility.
