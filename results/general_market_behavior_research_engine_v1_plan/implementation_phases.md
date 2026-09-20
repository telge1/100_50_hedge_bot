# Implementation Phases (after plan approval — not started now)

## Phase 0 — Contracts freeze
Lock semantic + temporal + leakage contracts; coverage gate CLI.

## Phase 1 — Replay & join pilot (BTC hours)
COMPLETE Full-OB hour + live trades/OI/liq; build 1s states offline to Parquet/FS first (no CH DDL required initially).

## Phase 2 — Feature pack mb_features_v1
Shape + footprint + quality; numeric asserts vs raw samples.

## Phase 3 — Episodes + outcomes
Detectors for P05/P08/P09/P16 first; attach horizons; purge/embargo tests.

## Phase 4 — Multi-day Track D200
OB200 Aug24–31 joint research window; prevalence stats.

## Phase 5 — Discovery & validation
Descriptive → rules; walk-forward; baselines only if N sufficient.

## Phase 6 — Optional CH materialization
Create derived tables from proposed_schema.sql after review.

## Explicit non-goals until later
Live classifier, dashboard, trading hooks, DL models, Full-OB collector changes.
