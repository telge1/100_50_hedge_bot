from gate_policies_v1 import evaluate_policies


def test_r6_veto_over_r5() -> None:
    row = {
        "ema_validity": "VALIDATED_TRP_D60",
        "R5": True,
        "R6_sideways": True,
        "R3": True,
        "pool_validity": "OK",
        "first_variant_drop": "PASS_E",
    }
    pol = evaluate_policies(row, short_paused=False)
    assert pol["policy_p2_r6_veto"] == "SHORT_NO"
    assert pol["policy_p1_variant_e"] == "SHORT_OK"
