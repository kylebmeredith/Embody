"""
List COMP callbacks for Embody Manager UI.

Renders the externalization tree with expand/collapse, rollover
highlights, and clickable Strategy/Delete buttons. All styling
is pure Python -- no TOP textures needed.

me   - this callbacks DAT
comp - the List COMP (available in all callbacks)
"""
from datetime import datetime, timezone

# --- Column indices ---
# Timestamp column hidden (2026-05-19) -- it showed the last _scanAndPopulate
# time, not last-save time, so every row had an identical value that
# updated together on every Refresh. Useless visual noise.
COL_EXPANDO = 0
COL_PATH = 1
COL_TYPE = 2
COL_FILE = 3
COL_SAVE = 4
COL_RELOAD = 5
COL_DELETE = 6

NUM_COLS = 7

HEADER_LABELS = [
	'', 'Network Path', '', 'External File Path',
	'Save', 'Reload', 'Del',
]
COL_WIDTHS    = [16, 0, 80, 200, 48, 60, 36]
COL_STRETCHES = [False, True, False, True, False, False, False]

# Map list columns to data fields for sorting.  Columns not in this map
# are not sortable (action buttons, expand-arrow).
SORTABLE_COLUMNS = {
	1: 'path',           # COL_PATH -- sort by op name
	2: 'type',           # COL_TYPE
	3: 'rel_file_path',  # COL_FILE
	4: 'row_state',      # COL_SAVE -- sort by dirtiness
}

# Material Design Icons codepoints (private-use area U+F0000–U+F1FFFF).
# The font must be set to 'Material Design Icons' on the rendering cell.
# Same font used by toolbar buttons -- see container_left/save_comp etc.
SAVE_GLYPH = '\U000f0193'    # MDI content-save
RELOAD_GLYPH = '\U000f0450'  # MDI refresh
ICON_FONT = 'Material Design Icons'

# Row states that mark a COMP / DAT as dirty (save is meaningful) vs saved.
DIRTY_STATES = {'Dirty', 'ParChange'}
SAVED_STATES = {'Saved'}
TEXT_PAD_X = 6  # horizontal padding for left-justified cells

# Row whose state cell is "active" (menu open) -- shows "..." while menu visible
_active_state_row = -1

# Persistent selection -- tracks the selected operator path (survives refresh/reorder)
_selected_path = ''

# --- Theme colors (loaded from Embody UI pars in onInitTable) ---
_t = {}


def _par4(name):
	"""Read an RGBA color tuple from Embody's UI parameters."""
	p = parent.Embody.par
	return (
		getattr(p, name + 'r').eval(),
		getattr(p, name + 'g').eval(),
		getattr(p, name + 'b').eval(),
		getattr(p, name + 'a').eval(),
	)


def _composite(fg, bg):
	"""Alpha-composite fg over bg, return opaque RGBA."""
	a = fg[3]
	return (
		a * fg[0] + (1 - a) * bg[0],
		a * fg[1] + (1 - a) * bg[1],
		a * fg[2] + (1 - a) * bg[2],
		1.0,
	)


def _brighten(color, amount=0.06):
	return (
		min(1.0, color[0] + amount),
		min(1.0, color[1] + amount),
		min(1.0, color[2] + amount),
		color[3],
	)


def _load_theme():
	"""Read colors from Embody UI parameters into _t cache."""
	global _t
	text = _par4('Textcolor')
	row = _par4('Listrowcolor')
	header = _par4('Listheadercolor')
	select = _par4('Listrowselectcolor')
	saved_raw = _par4('Savedcolor')
	btn = _par4('Buttonbackgroundcolor')

	# Pre-composite saved color (may have alpha < 1) over row bg
	saved = _composite(saved_raw, row) if saved_raw[3] < 1.0 else saved_raw

	# Amber for exporting -- warm shift from saved
	amber = (saved[0] + 0.12, max(0, saved[1] - 0.02),
	         max(0, saved[2] - 0.04), 1.0)

	# Comp state button -- slightly brighter than row bg for subtle visibility
	comp_bg = _brighten(row, 0.08)

	# State colors from Embody parameters
	dirty_raw = _par4('Dirtycolor')
	dirty = _composite(dirty_raw, row) if dirty_raw[3] < 1.0 else dirty_raw

	par_change_raw = _par4('Dirtyparcolor')
	par_change = _composite(par_change_raw, row) if par_change_raw[3] < 1.0 else par_change_raw

	tdn_saved_raw = _par4('Tdnsavedcolor')
	tdn_saved = _composite(tdn_saved_raw, row) if tdn_saved_raw[3] < 1.0 else tdn_saved_raw

	# TDN exporting -- warm shift from TDN saved blue
	tdn_amber = (tdn_saved[0] + 0.12, max(0, tdn_saved[1] - 0.02),
	             max(0, tdn_saved[2] - 0.04), 1.0)

	# Subtle column separator -- just visible enough to delineate columns
	border = _brighten(row, 0.04)

	_t.update({
		'text': text,
		'text_dim': (text[0] * 0.55, text[1] * 0.55, text[2] * 0.55, 1.0),
		'row': row,
		'row_alt': _brighten(row, 0.015),
		'header': header,
		'select': select,
		'saved': saved,
		'saved_roll': _brighten(saved, 0.08),
		'comp': comp_bg,
		'comp_roll': _brighten(comp_bg, 0.06),
		'amber': amber,
		'amber_roll': _brighten(amber, 0.08),
		'dirty': dirty,
		'dirty_roll': _brighten(dirty, 0.08),
		'par_change': par_change,
		'par_change_roll': _brighten(par_change, 0.08),
		'tdn_saved': tdn_saved,
		'tdn_saved_roll': _brighten(tdn_saved, 0.08),
		'tdn_amber': tdn_amber,
		'tdn_amber_roll': _brighten(tdn_amber, 0.08),
		'border': border,
	})


def _ensure_theme():
	"""Lazy-load theme if _t was reset by module recompile."""
	if not _t:
		_load_theme()


def clearActiveStrategy():
	"""Clear the active state row (called when menu closes). Name kept for
	parexec/callbacks compatibility."""
	global _active_state_row
	_active_state_row = -1


def _source():
	"""Return the inject_parents DAT (data source)."""
	return op('inject_parents')


def _row_bg(row):
	return _t['row'] if row % 2 == 0 else _t['row_alt']


def _apply_cell(attribs, row, col, data, highlight=False):
	"""Style a single data cell. Used by onInitCell and rollover restore."""
	_ensure_theme()
	if row >= data.numRows:
		return
	path = data[row, 'path'].val
	is_selected = (_selected_path and path == _selected_path)
	bg = _t['select'] if (highlight or is_selected) else _row_bg(row)

	if col == COL_EXPANDO:
		hc = data[row, 'has_children'].val == '1'
		if hc:
			expanded = parent.Embody.fetch('expanded_paths', set())
			attribs.text = '\u2212' if path in expanded else '+'
			attribs.textJustify = JustifyType.CENTER
		else:
			attribs.text = ''
		attribs.bgColor = bg

	elif col == COL_PATH:
		name = (path.rsplit('/', 1)[-1] or path) if path else ''
		attribs.text = name
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_TYPE:
		attribs.text = data[row, 'type'].val
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg

	elif col == COL_FILE:
		attribs.text = data[row, 'rel_file_path'].val
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_SAVE:
		st = data[row, 'row_state'].val
		# Save cell encodes both action affordance AND state-color (no separate
		# State column). bgColor: red for Dirty, amber for ParChange, dim
		# for Saved, transparent for unexternalized rows.
		if st == 'Dirty':
			attribs.text = SAVE_GLYPH
			attribs.textColor = _t['text']
			attribs.bgColor = _t['dirty']
		elif st == 'ParChange':
			attribs.text = SAVE_GLYPH
			attribs.textColor = _t['text']
			attribs.bgColor = _t['par_change']
		elif st == 'Saved':
			attribs.text = SAVE_GLYPH
			attribs.textColor = _t['text_dim']
			attribs.bgColor = bg
		elif st == 'Exporting':
			attribs.text = ''
			attribs.bgColor = _t['tdn_amber']
		else:
			attribs.text = ''
			attribs.bgColor = bg
		attribs.textJustify = JustifyType.CENTER
		attribs.fontFace = ICON_FONT
		attribs.fontSizeX = 14

	elif col == COL_RELOAD:
		st = data[row, 'row_state'].val
		# Reload only makes sense for COMPs that have a .tdn on disk.
		if st in (DIRTY_STATES | SAVED_STATES):
			attribs.text = RELOAD_GLYPH
			attribs.textColor = _t['text_dim']
			attribs.bgColor = bg
		else:
			attribs.text = ''
			attribs.bgColor = bg
		attribs.textJustify = JustifyType.CENTER
		attribs.fontFace = ICON_FONT
		attribs.fontSizeX = 14

	elif col == COL_DELETE:
		has_ext = bool(data[row, 'rel_file_path'].val)
		if has_ext:
			attribs.text = '×'
			attribs.fontSizeX = 12
			attribs.textColor = _t['text_dim']
		else:
			attribs.text = ''
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg


# -- Init callbacks -----------------------------------------------------------

def onInitTable(comp, attribs):
	_load_theme()
	attribs.bgColor = _t['row']
	attribs.textColor = _t['text']
	attribs.fontSizeX = 9
	attribs.sizeInPoints = True
	attribs.rowHeight = 20
	attribs.textJustify = JustifyType.CENTERLEFT


def onInitCol(comp, col, attribs):
	_ensure_theme()
	if col < len(COL_WIDTHS):
		attribs.colWidth = COL_WIDTHS[col]
		attribs.colStretch = COL_STRETCHES[col]
	# Subtle 1px right border on every column except the last
	if col < NUM_COLS - 1:
		attribs.rightBorderInColor = _t['border']


def onInitRow(comp, row, attribs):
	_ensure_theme()
	if row == 0:
		attribs.bgColor = _t['header']
		return
	data = _source()
	if data and row < data.numRows:
		depth = int(data[row, 'depth'].val or '0')
		attribs.rowIndent = depth * 18
	attribs.bgColor = _row_bg(row)


def onInitCell(comp, row, col, attribs):
	if row == 0:
		label = HEADER_LABELS[col] if col < len(HEADER_LABELS) else ''
		# Show ↑ / ↓ next to the header label of the active sort column.
		state = parent.Embody.fetch('sort_state', None, search=False)
		if state and SORTABLE_COLUMNS.get(col) == state.get('col'):
			label = f"{label} {'↑' if state.get('dir', 1) == 1 else '↓'}"
		attribs.text = label
		attribs.textJustify = JustifyType.CENTER
		return
	data = _source()
	if data and row < data.numRows:
		_apply_cell(attribs, row, col, data)


# -- Interaction callbacks ----------------------------------------------------

def onRollover(comp, row, col, coords, prevRow, prevCol, prevCoords):
	_ensure_theme()
	data = _source()
	if not data:
		return
	ncols = min(NUM_COLS, comp.par.cols.eval())

	# Row changed -> restore old row, highlight new row
	if prevRow != row:
		if prevRow is not None and prevRow > 0 and prevRow < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[prevRow, c],
				            prevRow, c, data, highlight=False)
		if row is not None and row > 0 and row < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[row, c],
				            row, c, data, highlight=True)

	# Column changed within same row -> restore old cell to highlight state
	if prevRow == row and prevCol != col and row is not None and row > 0:
		if prevCol >= 0 and prevCol < ncols and row < data.numRows:
			_apply_cell(comp.cellAttribs[row, prevCol],
			            row, prevCol, data, highlight=True)

	if row is None or col is None or row <= 0 or row >= data.numRows or col < 0:
		return

	if col == COL_TYPE:
		# Brighten to hint that clicking opens the network editor
		comp.cellAttribs[row, col].bgColor = _brighten(_t['select'], 0.04)
	elif col == COL_FILE:
		# Brighten to hint that clicking reveals the file
		if data[row, 'rel_file_path'].val:
			comp.cellAttribs[row, col].bgColor = _brighten(_t['select'], 0.04)
	elif col == COL_DELETE:
		if data[row, 'rel_file_path'].val:
			comp.cellAttribs[row, col].textColor = _t['text']
			comp.cellAttribs[row, col].bgColor = _t['select']

	elif col == COL_SAVE:
		st = data[row, 'row_state'].val
		if st in (DIRTY_STATES | SAVED_STATES):
			# Brighten so the user sees the cell is actionable
			comp.cellAttribs[row, col].textColor = _t['text']
			comp.cellAttribs[row, col].bgColor = _t['select']

	elif col == COL_RELOAD:
		st = data[row, 'row_state'].val
		if st in (DIRTY_STATES | SAVED_STATES):
			comp.cellAttribs[row, col].textColor = _t['text']
			comp.cellAttribs[row, col].bgColor = _t['select']


def onSelect(comp, startRow, startCol, startCoords,
             endRow, endCol, endCoords, start, end):
	if not end:
		return
	if startRow != endRow or startCol != endCol:
		return

	# Header click toggles column sort: asc -> desc -> off, three-state.
	if startRow == 0:
		field = SORTABLE_COLUMNS.get(startCol)
		if not field:
			return
		state = parent.Embody.fetch('sort_state', None, search=False)
		if state and state.get('col') == field:
			if state.get('dir', 1) == 1:
				new_state = {'col': field, 'dir': -1}
			else:
				new_state = None  # third click clears sort
		else:
			new_state = {'col': field, 'dir': 1}
		if new_state is None:
			parent.Embody.unstore('sort_state')
		else:
			parent.Embody.store('sort_state', new_state)
		# Recook the data source and reset the list to repaint.
		parent.Embody.op('list/inject_parents').cook(force=True)
		comp.par.reset.pulse()
		return

	if startRow <= 0:
		return

	row, col = startRow, startCol
	data = _source()
	if not data or row >= data.numRows:
		return

	path = data[row, 'path'].val
	ncols = min(NUM_COLS, comp.par.cols.eval())

	# Clear old selection and repaint that row
	global _selected_path
	prev_path = _selected_path
	_selected_path = ''
	if prev_path and prev_path != path:
		for r in range(1, data.numRows):
			if data[r, 'path'].val == prev_path:
				for c in range(ncols):
					_apply_cell(comp.cellAttribs[r, c], r, c, data)
				break

	# Set new selection and repaint it
	_selected_path = path
	for c in range(ncols):
		_apply_cell(comp.cellAttribs[row, c], row, c, data)

	if col in (COL_EXPANDO, COL_PATH):
		hc = data[row, 'has_children'].val == '1'
		if hc:
			expanded = parent.Embody.fetch('expanded_paths', set())
			expand_order = parent.Embody.fetch('expand_order', [])
			if path in expanded:
				expanded.discard(path)
				if path in expand_order:
					expand_order.remove(path)
			else:
				expanded.add(path)
				if path in expand_order:
					expand_order.remove(path)
				expand_order.append(path)
			parent.Embody.store('expanded_paths', expanded)
			parent.Embody.store('expand_order', expand_order)
			parent.Embody.Refresh()

	elif col == COL_TYPE:
		oper = op(path)
		if oper:
			for sibling in oper.parent().findChildren(depth=1):
				sibling.selected = False
			oper.selected = True
			pane = ui.panes.createFloating(
				type=PaneType.NETWORKEDITOR, name=oper.name,
				maxWidth=1920, maxHeight=1080,
				monitorSpanWidth=0.9, monitorSpanHeight=0.9)
			pane.owner = oper.parent()
			pane.home(zoom=True, op=oper)
			pane.zoom = 2
			pane.x = oper.nodeCenterX
			pane.y = oper.nodeCenterY

	elif col == COL_FILE:
		rel_fp = data[row, 'rel_file_path'].val
		if rel_fp:
			parent.Embody.OpenSaveFile(rel_fp)

	elif col == COL_SAVE:
		st = data[row, 'row_state'].val
		oper = op(path)
		if oper is None or st not in (DIRTY_STATES | SAVED_STATES):
			return
		# Direct save -- the dialog cascade is overkill for the common case.
		ext = parent.Embody.ext.Embody
		if oper.family == 'COMP':
			ext.Save(oper.path)
		elif oper.family == 'DAT':
			ext._saveOpFromMenu(oper)

	elif col == COL_RELOAD:
		st = data[row, 'row_state'].val
		oper = op(path)
		if oper is None or oper.family != 'COMP':
			return
		if st not in (DIRTY_STATES | SAVED_STATES):
			return
		# Reload offers .tdn vs .tox; hand off to the sub-menu.
		parent.Embody.ext.Embody._actionMenuReload(oper)

	elif col == COL_DELETE:
		rel_fp = data[row, 'rel_file_path'].val
		if not rel_fp:
			return
		oper = op(path)
		result = ui.messageBox(
			'Remove',
			'Remove this externalization?\n\n'
			'This will delete the external file from disk, clear the\n'
			"operator's externalization parameter (par.externaltox or\n"
			'par.file), and remove the tracking entry. Cannot be undone.\n\n'
			'Operator: ' + path,
			buttons=['Cancel', 'Remove'])
		if result == 1:
			parent.Embody.RemoveListerRow(path, rel_fp)


def onRadio(comp, row, col, prevRow, prevCol):
	return


def onFocus(comp, row, col, prevRow, prevCol):
	return


def onEdit(comp, row, col, val):
	return
