# Changelog

## 0.2.6

Clarifies weekday numbering and exact-label schedule selection. Normalizes an
explicit album playback request without a search query. Adds a scoped constraint
for whole, simple English alarm/timer stop and book/audiobook resume commands
while delegating through the router. Matching Household Voice tools fill an
omitted category or reject conflicting arguments before acting. The constraint
is bound to the request context and device and is reset after delegation.
Direct fallback-agent requests and unrecognized wording bypass it; it is not a
guarantee that the model will invoke a tool or complete playback.

## 0.2.5

Adds an optional announcement conversation router. Exact start, cancel, and
status commands call the announcement runtime directly; other requests delegate
to the configured fallback conversation agent. The router starts disabled and
does not select itself in a voice pipeline. Replies distinguish queued work from
confirmed delivery.

Recognizes ESPHome device MAC addresses stored in Home Assistant registry
connections as well as identifiers, while preserving exact device/client and
encryption checks.

Adds a phone-presence policy selector: preserve the five-minute freshness default,
allow a one-hour report window, or explicitly trust the current HA home state.
Away, unknown, and unavailable phones remain excluded.

## 0.2.4

Uses compact, stable API display names so Home Assistant's merged tool names fit
OpenAI-compatible provider limits. Existing API identifiers and selections remain
valid. Actual-HA fixtures validate merged tool schemas before release.

## 0.2.3

Implements Home Assistant's LLM platform discovery callback. Household Voice
keeps its four explicitly selectable tool APIs; discovery does not duplicate
them into the built-in Assist API. The actual-HA fixture verifies this contract.

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
