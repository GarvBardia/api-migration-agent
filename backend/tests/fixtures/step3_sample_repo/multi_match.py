"""Two DIFFERENT deprecated calls in the same file.

Exercises the migration-0002 match-level granularity: this one file must
produce two distinct file_tasks rows sharing file_path/run_id but differing
in matched_symbol/line_start.
"""

import oldapi


def run_legacy():
    return oldapi.legacy_call(7)


def run_old_thing():
    return oldapi.old_thing(8)
