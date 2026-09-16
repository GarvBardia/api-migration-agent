"""Direct call -- Step 3's scanner assigns high confidence (0.95).

Expected to reach status='validated': 0.95 + 0.15 (capped at 1.0) stays
above DEFAULT_CONFIDENCE_THRESHOLD (0.8).
"""

import oldapi


def run_a(a, b):
    return oldapi.legacy_call(a, b)
