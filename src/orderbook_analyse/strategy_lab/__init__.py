"""Strategy Lab — StrategySpec models (P1) + schema/loader (P2).

P2 adds deterministic JSON Schema generation and a safe YAML raw loader.
No semantic validator, compiler, hashing, catalogs, or adapters yet.
"""

from orderbook_analyse.strategy_lab.loader import (
    StrategyYamlLoadError,
    load_strategy_yaml,
    load_strategy_yaml_path,
)
from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_SCHEMA_VERSION,
    StrategySpec,
)
from orderbook_analyse.strategy_lab.schema import (
    generate_strategy_spec_schema,
)

__all__ = [
    "STRATEGY_SPEC_SCHEMA_VERSION",
    "StrategySpec",
    "StrategyYamlLoadError",
    "generate_strategy_spec_schema",
    "load_strategy_yaml",
    "load_strategy_yaml_path",
]
