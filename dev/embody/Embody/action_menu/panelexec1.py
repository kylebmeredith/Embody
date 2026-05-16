"""Roll-off close for the action menu popup.

Fires when the cursor's rollover value on the action_menu container
transitions from 1 to 0 -- the user moved the mouse outside the popup.
Esc on the inner keyboardinDAT still works as a deliberate close.
"""

def onOffToOn(panelValue):
	return

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	try:
		parent.Embody.ext.Embody.CloseActionMenu()
	except Exception:
		pass

def whileOff(panelValue):
	return

def onValueChange(panelValue):
	return
