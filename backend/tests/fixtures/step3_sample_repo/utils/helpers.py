"""One call via a re-exported wrapper -- medium confidence.

``run_legacy`` is a module-level alias for ``oldapi.legacy_call``, resolved
by simple top-level dataflow (not a from-import or aliased-import), so it
lands in the "resolvable but through an alias or re-export" confidence tier
rather than the "direct" tier.
"""

import oldapi

run_legacy = oldapi.legacy_call


def call_it():
    return run_legacy(9)
