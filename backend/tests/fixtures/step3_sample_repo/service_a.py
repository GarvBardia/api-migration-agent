"""Two direct calls to the deprecated function -- high confidence."""

import oldapi


def run_first():
    return oldapi.legacy_call(1, 2)


def run_second():
    result = oldapi.legacy_call(3, 4)
    return result
