from input_verify_v1 import verify_inputs


def test_bt60_inputs_verify() -> None:
    v = verify_inputs()
    assert v["all_ok"] is True
    assert v["files"]["regime_gegenprobe_asof.csv"]["ok"] is True
