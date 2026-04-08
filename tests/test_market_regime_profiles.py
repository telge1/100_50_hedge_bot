import unittest
from datetime import datetime, timedelta, timezone

from strategy.market_regime import ProfileBuilder, ProfileUpdater


class FakeProfileStore:
    def __init__(self) -> None:
        self.active_symbols = ["BTCUSDT", "ETHUSDT"]
        self.grouped_rows = {
            "BTCUSDT": [
                {
                    "symbol": "BTCUSDT",
                    "timestamp": datetime.now(timezone.utc) - timedelta(minutes=2),
                    "price": 100.0,
                    "price_change_1m": 1.0,
                    "price_change_5m": 2.0,
                    "price_change_15m": 3.0,
                    "oi_change_ratio": 0.5,
                    "trade_volume_1m": 10.0,
                    "volume_spike_ratio": 1.2,
                    "buy_volume": 6.0,
                    "sell_volume": 4.0,
                    "delta": 2.0,
                    "orderflow_ratio": 0.2,
                    "liquidation_density_5m": 0.1,
                    "liquidation_cluster_score": 0.0,
                    "microburst_score": 0.3,
                    "spread": 0.05,
                    "trade_count_1m": 5.0,
                    "avg_trade_size": 2.0,
                    "atr_1m": 0.8,
                },
                {
                    "symbol": "BTCUSDT",
                    "timestamp": datetime.now(timezone.utc) - timedelta(minutes=1),
                    "price": 110.0,
                    "price_change_1m": -2.0,
                    "price_change_5m": 1.0,
                    "price_change_15m": 2.0,
                    "oi_change_ratio": -0.25,
                    "trade_volume_1m": 20.0,
                    "volume_spike_ratio": 2.2,
                    "buy_volume": 15.0,
                    "sell_volume": 5.0,
                    "delta": 10.0,
                    "orderflow_ratio": 0.6,
                    "liquidation_density_5m": 0.3,
                    "liquidation_cluster_score": 0.2,
                    "microburst_score": 1.5,
                    "spread": 0.11,
                    "trade_count_1m": 8.0,
                    "avg_trade_size": 2.5,
                    "atr_1m": 1.4,
                },
            ],
            "ETHUSDT": [],
        }
        self.upserted_profiles = []
        self.history_profiles = []

    def load_active_symbols(self):
        return list(self.active_symbols)

    def load_raw_feature_rows(self, *, symbols, start_time=None, end_time=None):
        return {symbol: list(self.grouped_rows.get(symbol, [])) for symbol in symbols}

    def upsert_coin_profiles(self, profiles):
        self.upserted_profiles.extend(profiles)

    def insert_coin_profile_history(self, profiles, snapshot_time=None):
        self.history_profiles.append((list(profiles), snapshot_time))


class MarketRegimeProfileTests(unittest.TestCase):
    def test_profile_builder_builds_stats_and_negative_price_tail(self) -> None:
        store = FakeProfileStore()
        builder = ProfileBuilder(store)

        result = builder.build_profiles(symbols=["BTCUSDT"])

        self.assertIn("BTCUSDT", result.profiles)
        profile = result.profiles["BTCUSDT"]
        price_stats = profile.get_stats("price_change_1m")
        self.assertEqual(profile.sample_size, 2)
        self.assertAlmostEqual(price_stats.mean, -0.5)
        self.assertAlmostEqual(price_stats.n99 or 0.0, -2.0)
        self.assertGreater(profile.get_stats("delta_ratio").p99, 0.0)
        self.assertGreater(profile.get_stats("spread_ratio").p99, 0.0)
        self.assertGreater(profile.get_stats("atr_1m").p99, 0.0)

    def test_profile_builder_persist_profiles_writes_current_and_history(self) -> None:
        store = FakeProfileStore()
        builder = ProfileBuilder(store)

        result = builder.build_profiles(symbols=["BTCUSDT"])
        builder.persist_profiles(result, write_history=True)

        self.assertEqual(len(store.upserted_profiles), 1)
        self.assertEqual(len(store.history_profiles), 1)

    def test_profile_updater_refresh_all_active_symbols_skips_empty_symbols(self) -> None:
        store = FakeProfileStore()
        updater = ProfileUpdater(ProfileBuilder(store))

        result = updater.refresh_all_active_symbols()

        self.assertIn("BTCUSDT", result.refreshed_symbols)
        self.assertIn("ETHUSDT", result.skipped_symbols)
        self.assertEqual(result.skipped_symbols["ETHUSDT"], "no_rows_in_window")


if __name__ == "__main__":
    unittest.main()
