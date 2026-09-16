"""Fake deprecated library, same convention as the Step 3/4/5 fixtures."""


def legacy_call(a, b):
    raise NotImplementedError("legacy_call is deprecated; use new_call instead")


def new_call(a, b):
    return a + b
