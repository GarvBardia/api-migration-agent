from service import configure_tls


def test_configure_tls_returns_true():
    assert configure_tls() is True
