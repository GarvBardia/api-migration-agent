"""Zero matches expected.

Defines a LOCAL function named ``legacy_call`` -- same bare name as the
target symbol, but never imported from ``oldapi``. A scanner that matches
on bare symbol name instead of resolving imports/scope would incorrectly
flag the call below; this fixture is the precision test for that failure
mode.
"""


def legacy_call(x):
    return x + 1


def use_it():
    return legacy_call(12)
