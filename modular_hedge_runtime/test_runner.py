import unittest

from modular_hedge_runtime.registry import list_strategy_names
from modular_hedge_runtime.runner import build_parser


class ModularRunnerTests(unittest.TestCase):
    def test_registry_contains_dynamic_breakeven(self) -> None:
        self.assertIn("dynamic_breakeven", list_strategy_names())
        self.assertIn("basket_exit", list_strategy_names())

    def test_parser_accepts_strategy_and_overrides(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--strategy",
                "dynamic_breakeven",
                "--symbol",
                "ethusdt",
                "--price-poll-interval",
                "2.5",
                "--reconcile-interval",
                "11",
            ]
        )

        self.assertEqual(args.strategy, "dynamic_breakeven")
        self.assertEqual(args.symbol, "ethusdt")
        self.assertEqual(args.price_poll_interval, 2.5)
        self.assertEqual(args.reconcile_interval, 11.0)


if __name__ == "__main__":
    unittest.main()
