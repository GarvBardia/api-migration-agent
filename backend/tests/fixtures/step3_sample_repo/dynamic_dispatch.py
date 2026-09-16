"""One call via getattr() dynamic dispatch -- low confidence.

The scanner cannot statically confirm ``oldapi`` (passed to ``getattr``) is
really the target module beyond this one call site, only that the string
literal names the target symbol -- so this must surface as a low-confidence
match, not be dropped or guessed high.
"""

import oldapi


def call_dynamically():
    return getattr(oldapi, "legacy_call")(10)
