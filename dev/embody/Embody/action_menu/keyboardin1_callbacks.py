"""Action menu keyboard handler.

Captures arrow keys (move selection), Enter (dispatch), Esc (close).
"""

def onKey(dat, key, character, alt, lAlt, rAlt, ctrl, lCtrl, rCtrl,
		  shift, lShift, rShift, state, time, cmd, lCmd, rCmd):
	if not state:
		return
	ext = parent.Embody.ext.Embody
	data = parent().op('action_items')
	if data is None:
		return

	n = max(1, data.numRows - 1)  # number of action rows
	current = max(1, int(parent().fetch('actionmenu_selected_row', 1) or 1))

	if key == 'up':
		new_sel = current - 1 if current > 1 else n
		parent().store('actionmenu_selected_row', new_sel)
		parent().op('list1').reset()
	elif key == 'down':
		new_sel = current + 1 if current < n else 1
		parent().store('actionmenu_selected_row', new_sel)
		parent().op('list1').reset()
	elif key in ('enter', 'return'):
		action_id = data[current, 'action_id'].val
		enabled = data[current, 'enabled'].val
		if enabled != '0':
			ext._dispatchAction(action_id)
	elif key in ('esc', 'escape'):
		ext.CloseActionMenu()

def onShortcut(dat, shortcutName, time):
	return
