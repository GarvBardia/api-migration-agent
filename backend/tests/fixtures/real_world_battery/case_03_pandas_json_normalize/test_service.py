from service import flatten


def test_flatten_produces_expected_columns():
    records = [{"a": {"b": 1}}, {"a": {"b": 2}}]
    df = flatten(records)
    assert list(df.columns) == ["a.b"]
    assert df["a.b"].tolist() == [1, 2]
