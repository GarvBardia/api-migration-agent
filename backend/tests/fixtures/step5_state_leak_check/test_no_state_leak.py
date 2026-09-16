"""Run through the sandbox directly, twice in a row (two separate container
invocations), to prove containers are torn down between runs with no
filesystem state leaking from one into the next.

The marker path is deliberately OUTSIDE the mounted /repo volume (which is
recreated fresh from a temp dir per attempt anyway) -- it lives in the
container's own root filesystem, which --rm discards completely on exit.
If a previous container's filesystem somehow persisted, this test would
fail on the second invocation because the marker would already exist.

(Installed-package leakage isn't separately tested: with --network none,
pip install of anything from the internet is impossible in the first
place, so there's nothing for a "did a pip-installed package leak" test to
even exercise -- the network isolation already forecloses it.)
"""

import os

MARKER_PATH = "/tmp/step5_leak_marker_should_never_persist.txt"


def test_marker_absent_then_created_fresh_each_run():
    assert not os.path.exists(MARKER_PATH), (
        "state leaked from a previous sandbox container run -- marker file "
        "should not exist in a fresh container"
    )
    with open(MARKER_PATH, "w") as f:
        f.write("created in this container\n")
