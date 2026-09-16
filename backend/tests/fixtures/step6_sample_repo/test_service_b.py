from service_b import run_b


def test_run_b_returns_correct_sum():
    assert run_b(4, 5) == 9
