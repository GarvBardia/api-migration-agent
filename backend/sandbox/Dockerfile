# Validation Agent sandbox image (Step 5).
#
# Ephemeral containers built from this image run with --network none, so
# nothing can `pip install` at run time -- the test runner has to already be
# in the image. Rebuild (docker build -t migration-agent-sandbox:latest
# backend/sandbox) whenever these pinned versions change.
#
# Added 2026-09-16 for the 10-case real-world battery
# (tests/REAL_WORLD_BATTERY_REPORT.md, see CLAUDE.md's dated note): each
# case's sandboxed test run needs the case's TARGET (post-migration)
# library actually importable -- validation runs the MIGRATED code
# against real tests, not a stub. Pinned to the version each case's
# breaking change was introduced in (the NEW side of the migration),
# matching how a real project pinning post-upgrade would look.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-json-report==1.5.0 \
    openai==1.51.0 \
    pandas==2.2.3 \
    numpy==2.1.2 \
    pydantic==2.9.2 \
    sqlalchemy==2.0.35 \
    flask==3.0.3 \
    markupsafe==2.1.5 \
    urllib3==2.2.3

WORKDIR /repo
