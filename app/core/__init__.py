"""Pure domain logic.

Nothing in this package may touch the network, the disk or the clock. Every
function takes its inputs explicitly — including the current time, as an
explicit timezone-aware argument — so that all of it is unit-testable and free
of lookahead by construction (CLAUDE.md 1.1).
"""
