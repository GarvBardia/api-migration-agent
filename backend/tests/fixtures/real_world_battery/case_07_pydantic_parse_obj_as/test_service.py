from service import parse_int_list


def test_parse_int_list_coerces_strings():
    assert parse_int_list(["1", "2", "3"]) == [1, 2, 3]
