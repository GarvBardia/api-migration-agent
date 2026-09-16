import pytest
from pydantic import ValidationError

from service import Item


def test_valid_name_accepted():
    assert Item(name="widget").name == "widget"


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        Item(name="   ")
