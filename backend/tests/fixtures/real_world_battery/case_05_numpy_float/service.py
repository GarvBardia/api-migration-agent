"""numpy.float (a deprecated alias for the Python builtin float) was
removed in NumPy 1.24 -- raises AttributeError on access."""

import numpy as np


def to_float(x):
    return np.float(x)
