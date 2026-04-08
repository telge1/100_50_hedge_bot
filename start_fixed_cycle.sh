#!/usr/bin/env bash

# Run fixed-cycle strategy via the modular fixed_cycle runner.
# Make sure env keys are set (see /strategy/env or /env/local.env) before launching.
cd "$(dirname "$0")"
python -m fixed_cycle_hedge_bot.runner --strategy fixed_cycle --strategy-config-file fixed_cycle_hedge_bot/config/fixed_cycle_config.json
