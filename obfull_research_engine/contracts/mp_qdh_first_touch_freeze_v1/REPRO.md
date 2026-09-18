# Repro — mp-qdh-first-touch-freeze-v1

1. Checkout tag `mp-qdh-first-touch-freeze-v1-20260918`
2. Set PYTHONPATH to `obfull_research_engine/src` and `orderbook_analyse/src`
3. Offline: `pytest ...test_mp_qdh_first_touch_freeze_v1_offline.py`
4. Smoke: `python -m obfull_research_engine.mp_qdh_first_touch_study_v1 --smoke-only`
5. Expect contract_hash in CONTRACT_MANIFEST.json; old `2bbd0ec0…` checkpoints rejected
6. Historical 115-event results remain under runs/mp_qdh_first_touch_study_v1_20260917 (legacy contract); no silent rehash
