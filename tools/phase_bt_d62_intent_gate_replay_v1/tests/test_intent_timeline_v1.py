from intent_timeline_v1 import build_intent_timeline, MANDATORY_INTENTS


def test_mandatory_intents_present() -> None:
    ts = {r["decision_close_utc"] for r in build_intent_timeline()}
    for m in MANDATORY_INTENTS:
        assert m["decision_close_utc"] in ts
