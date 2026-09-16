from service import bold


def test_bold_wraps_in_tag():
    assert str(bold("hi")) == "<b>hi</b>"
