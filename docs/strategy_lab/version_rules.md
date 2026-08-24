# Strategy Lab — Version Rules

This document is the single source of truth for schema, catalog, strategy, universe,
and provenance versioning in Phase 1.

## Schema version (`strategy_spec/vN`)

| Version | Meaning |
|---------|---------|
| `strategy_spec/v1` | Legacy StrategySpec root (`StrategySpec` / `StrategySpecV1`) |
| `strategy_spec/v2` | Composable StrategySpec V2 root (`StrategySpecV2`) |

- A **new schema version** is required for incompatible structure or meaning changes
  of the strategy document model.
- Existing strategy files are **never silently reinterpreted** under a newer schema.
- There is **no automatic V1→V2 migration** and **no silent V1 fallback** in the
  V2 loader/decoder path. `metadata.schema_version` must equal `strategy_spec/v2`.

Schema version is independent of catalog/plugin contract version.

## Catalog / plugin contract version (`catalog/vN`)

| Version | Meaning |
|---------|---------|
| `catalog/v1` | Typed V1 catalogs (legacy Phase-1 catalog surface) |
| `catalog/v2` | Closed V2 feature, operator, and plugin registries |

- Schema version and catalog version are **separate**.
- `catalog/v2` versions feature, operator, and plugin contracts together.
- Incompatible plugin/feature/operator semantics require a **new contract version**.
- An existing `(plugin_id, contract_version)` pair must **not** silently change meaning.
- Changes to required parameters, feature bindings, entry timing, or data requirements
  are semantically relevant and need an explicit contract bump when incompatible.

## Strategy files and goldens

- A semantic strategy change produces new canonical JSON bytes and a new SHA256.
- Golden files (`*.canonical.json` and `*.sha256`) must be updated **together**.
- After safe YAML load + V2 decode, formatting/key order alone must not change meaning;
  the compiler emits lexicographically sorted canonical JSON.
- The strategy hash identifies the **fully frozen** configuration (including universe
  and provenance fields present in the strategy document).

## Universe and provenance

- `universe.version` and `universe.content_hash` identify the universe content in use.
- `content_hash` is `sha256:` plus SHA256 of the **exact committed file bytes** of the
  universe JSON (not a normalized symbol-list digest).
- Provenance binds source git commit, repository, source paths, catalog contract,
  plugin refs, and causality status.
- Runtime host data, timestamps, and result artifacts do **not** belong in strategy
  provenance.

## Change decision table

| Änderung | erforderliche Aktion |
|----------|----------------------|
| inkompatible Strategy-Struktur | neue Schema-Version |
| inkompatible Feature-/Operator-/Plugin-Semantik | neue Catalog-/Contract-Version |
| Parameterwert einer Strategie geändert | neuer Strategy-Hash + neue Goldens |
| nur Dokumentation ohne Semantikänderung | kein neuer Strategy-Hash |
| Universe-Datei geändert | neue Universe-Version/content_hash + neuer Strategy-Hash |
