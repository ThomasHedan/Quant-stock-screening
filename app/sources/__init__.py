"""External data sources.

Everything that talks to the outside world lives here. Free APIs break,
throttle and lie, so each module in this package is written on the assumption
that its source is hostile: bounded timeouts, an explicit retry policy, a
logged failure mode, and validation of every field before the data is allowed
any further into the app (CLAUDE.md 1.1).
"""
