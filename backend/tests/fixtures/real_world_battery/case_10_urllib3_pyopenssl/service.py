"""urllib3 v2 dropped pyOpenSSL support entirely (stdlib ssl now handles
SNI universally) -- urllib3.contrib.pyopenssl, including
inject_into_urllib3(), no longer exists. There's no direct 1:1
replacement call; the fix is to stop calling it (urllib3 v2 doesn't need
it), which makes this case a genuine judgment call for the Migration
Agent, not a mechanical rename."""

from urllib3.contrib.pyopenssl import inject_into_urllib3


def configure_tls():
    inject_into_urllib3()
    return True
