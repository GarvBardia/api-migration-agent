"""Run through the sandbox directly (not via a file_task) to prove
--network none actually disables networking.

With --network none there is no non-loopback network interface in the
container's netns at all, so any external connection attempt fails
immediately with an OSError (e.g. ENETUNREACH) regardless of the host's
firewall rules -- a stronger, more universal signal than relying on a
specific port being blocked.
"""

import socket

import pytest


def test_external_connection_is_unreachable():
    with pytest.raises(OSError):
        sock = socket.create_connection(("8.8.8.8", 53), timeout=3)
        sock.close()
