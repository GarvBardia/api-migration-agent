from service import Widget


def test_widget_has_expected_table_name():
    assert Widget.__tablename__ == "widgets"


def test_widget_is_constructible():
    w = Widget(id=1, name="bolt")
    assert w.name == "bolt"
