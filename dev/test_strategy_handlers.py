"""Deprecated: tests targeted the removed TOX/TDN strategy split.

Every externalized COMP now writes both .tox and .tdn unconditionally,
so HandleStrategySwitch / HandleStrategyRemove / Toxtag / Tdntag no
longer exist.  This file is kept as a stub to avoid breaking any DAT
that still references it; it intentionally defines no test cases.
"""
