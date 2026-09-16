"""One call via an aliased import -- resolvable, but through an alias."""

from oldapi import legacy_call as lc


def run():
    return lc(5, 6)
