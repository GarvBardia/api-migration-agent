"""pandas.io.json.json_normalize was removed in pandas 1.0 -- moved to the
top-level pandas namespace as pandas.json_normalize."""

from pandas.io.json import json_normalize


def flatten(records):
    return json_normalize(records)
