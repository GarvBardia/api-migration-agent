from service_a import run_a


def test_run_a_returns_correct_sum():
    assert run_a(2, 3) == 5
