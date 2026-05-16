"""Action menu listCOMP callbacks.

Driven by a sibling tableDAT (action_items) with columns:
  label, action_id, enabled

Row 0 is the header. Rows 1+ are clickable action items.
"""

ROW_HEIGHT = 26
TEXT_PAD_X = 10

# Theme colors (read from Embody UI pars at init)
_t = {}

def _load_theme():
	global _t
	p = parent.Embody.par
	def rgba(name):
		return (
			getattr(p, name + 'r').eval(),
			getattr(p, name + 'g').eval(),
			getattr(p, name + 'b').eval(),
			getattr(p, name + 'a').eval(),
		)
	def rgb_opaque(rgba_t):
		return (rgba_t[0], rgba_t[1], rgba_t[2])
	bg = rgba('Backgroundcolor')
	row = rgba('Listrowcolor')
	sel = rgba('Listrowselectcolor')
	txt = rgba('Textcolor')
	_t['bg'] = rgb_opaque(bg)
	_t['row'] = rgb_opaque(row)
	_t['sel'] = rgb_opaque(sel)
	_t['text'] = rgb_opaque(txt)
	_t['text_dim'] = (txt[0] * 0.5, txt[1] * 0.5, txt[2] * 0.5)

def _data():
	return parent().op('action_items')

def _selected_row():
	"""Row currently highlighted via keyboard / rollover. 0 = none."""
	return int(parent().fetch('actionmenu_selected_row', 1) or 1)

def _set_selected(row):
	parent().store('actionmenu_selected_row', max(1, int(row)))

def onInitTable(comp, attribs):
	_load_theme()
	attribs.bgColor = _t['row']
	attribs.textColor = _t['text']
	attribs.fontSizeX = 10
	attribs.sizeInPoints = True
	attribs.rowHeight = ROW_HEIGHT
	attribs.textJustify = JustifyType.CENTERLEFT

def onInitRow(comp, row, attribs):
	if not _t:
		_load_theme()
	data = _data()
	if data is None or row >= data.numRows:
		return
	# Header row is hidden -- listCOMP shows row 0 as data row 0 by default,
	# so we want to render row 0 as the first action (skip table header).
	# We solve this by sourcing only data rows: rows = action_items.numRows - 1.
	# But to keep things simple, treat list row N = data row N+1.
	sel = _selected_row()
	if row + 1 == sel:
		attribs.bgColor = _t['sel']
	else:
		attribs.bgColor = _t['row']

def onInitCell(comp, row, col, attribs):
	data = _data()
	if data is None:
		attribs.text = ''
		return
	data_row = row + 1  # skip table header
	if data_row >= data.numRows:
		attribs.text = ''
		return
	enabled = data[data_row, 'enabled'].val
	label = data[data_row, 'label'].val
	attribs.text = label
	attribs.textJustify = JustifyType.CENTERLEFT
	attribs.textOffsetX = TEXT_PAD_X
	if enabled == '0':
		attribs.textColor = _t['text_dim']
	else:
		attribs.textColor = _t['text']

def onInitCol(comp, col, attribs):
	attribs.colStretch = True

def onRollover(comp, row, col, coords, prevRow, prevCol, prevCoords):
	data = _data()
	if data is None or row is None or row < 0:
		return
	if row + 1 >= data.numRows:
		return
	_set_selected(row + 1)
	comp.reset()

def onClickRow(comp, row, col, coords, prevRow, prevCol, prevCoords):
	data = _data()
	if data is None or row < 0 or row + 1 >= data.numRows:
		return
	data_row = row + 1
	enabled = data[data_row, 'enabled'].val
	if enabled == '0':
		return
	action_id = data[data_row, 'action_id'].val
	parent.Embody.ext.Embody._dispatchAction(action_id)
