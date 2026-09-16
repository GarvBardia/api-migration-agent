from service import make_sparse


def test_make_sparse_roundtrips_values():
    result = make_sparse({"x": [0, 0, 1, 0, 2]})
    assert list(result["x"]) == [0, 0, 1, 0, 2]
