"""One clock helper shared by the audit, draft and booking stores."""

import datetime as dt


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()
