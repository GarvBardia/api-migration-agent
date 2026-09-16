"""Aliased-import call -- Step 3's scanner assigns medium confidence (0.6).

Expected to reach status='needs_review' via the Phase 0 confidence gate:
0.6 + 0.15 = 0.75, still below DEFAULT_CONFIDENCE_THRESHOLD (0.8), even
though its tests pass on the first attempt.

`import oldapi` is included alongside the aliased import so a single-line
patch of the call site (line_start==line_end, matching Step 3/4/5's
line-span patch model) can reference `oldapi.new_call` directly without
also needing to rewrite the import statement -- rewriting multi-line spans
is out of scope for this pipeline.
"""

import oldapi
from oldapi import legacy_call as lc


def run_b(a, b):
    return lc(a, b)
