"""pandas.SparseDataFrame/SparseSeries were removed in pandas 1.0 --
replaced by the .sparse accessor on a regular DataFrame/Series backed by
a SparseDtype column."""

import pandas as pd


def make_sparse(data):
    return pd.SparseDataFrame(data)
