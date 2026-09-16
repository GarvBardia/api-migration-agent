from service import to_float


def test_to_float_converts_string():
    assert to_float("3.5") == 3.5
