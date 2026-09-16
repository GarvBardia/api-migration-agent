"""pydantic v1's parse_obj_as(Type, data) is deprecated in v2 in favor of
TypeAdapter(Type).validate_python(data)."""

from typing import List

from pydantic import parse_obj_as


def parse_int_list(data):
    return parse_obj_as(List[int], data)
