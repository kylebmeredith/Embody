"""Focus / re-entry panelexec for the Embody COMP.

When the user moves the mouse into the Embody manager panel, pulse a
Refresh so the lister and dirty markers reflect the current par-driven
scan.  TD's panel attributes don't expose a `focus` value, so we use
`inside` (mouse entered) as the proxy for "user is interacting with
this panel again" -- functionally equivalent for the manager UX since
the user has to bring the mouse over to click anything anyway.

Throttled with a stored last-fire timestamp so rapid in/out hovers
(e.g. mouse skimming the edge) don't trigger a Refresh storm.

Wiring:
  This file is the syncfile target for a panelexec DAT placed inside
  the Embody COMP.  The DAT's `panels` parameter points at the Embody
  COMP itself, and `panelvalue` is `inside`.  Off-to-On is enabled so
  the callback fires when the mouse enters.

me   - this panelexec DAT
me.parent() - the Embody COMP
"""

import time

# Minimum seconds between re-entry Refresh calls.  Short enough to
# feel responsive when the user comes back from another window;
# long enough that flicking the mouse across the panel doesn't queue
# multiple Refreshes.
THROTTLE_SECONDS = 2.0


def _maybe_refresh():
	emb = me.parent()
	now = time.time()
	last = emb.fetch('_focus_refresh_last', 0.0, search=False)
	if now - last < THROTTLE_SECONDS:
		return
	emb.store('_focus_refresh_last', now)
	# Skip while Embody is in Perform Mode -- Refresh is a no-op there
	# but we save the storage write too.
	if emb.ext.Embody._performMode:
		return
	emb.Refresh()


def onOffToOn(panelValue):
	# Fires when the watched panel value flips from False to True.
	# For panelvalue='inside' that means the mouse just entered the panel.
	_maybe_refresh()


def whileOn(panelValue):
	return


def onOnToOff(panelValue):
	return


def whileOff(panelValue):
	return


def onValueChange(panelValue, prev):
	# Belt-and-suspenders: some builds prefer Value Change over OffToOn.
	if panelValue.val:
		_maybe_refresh()
