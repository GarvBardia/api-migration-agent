from service import run


def test_run_returns_correct_sum():
    """Passes only once run() calls oldapi.new_call instead of legacy_call --
    legacy_call raises NotImplementedError, new_call returns a + b."""

    assert run(2, 3) == 5
