"""
Prepare hierarchical data for the Manager List COMP.

Takes the raw externalizations select DAT as input and:
1. Injects synthetic parent rows for tree hierarchy
2. Computes depth, has_children for each row
3. Filters by expanded_paths for tree expand/collapse
4. Adds row_state column (Saved / Dirty / ParChange / Exporting / Comp)
5. Sorts hierarchically (parents before children, alphabetical)

Output columns:
  path, type, rel_file_path, timestamp, build, touch_build,
  row_state, depth, has_children
"""

MAX_VISIBLE_ROWS = 100


def onCook(scriptOp):
	scriptOp.clear()
	inp = scriptOp.inputs[0]

	if not inp or inp.numRows == 0:
		return

	in_headers = [c.val for c in inp.row(0)]
	path_idx = in_headers.index('path')
	type_idx = in_headers.index('type')

	# Build data rows dict keyed by path
	data_rows = {}
	comp_paths = set()

	for i in range(1, inp.numRows):
		row = {h: inp[i, j].val for j, h in enumerate(in_headers)}
		path = row['path']
		data_rows[path] = row
		oper = op(path)
		if oper and oper.family == 'COMP':
			comp_paths.add(path)

	# Inject synthetic parent rows for tree hierarchy
	all_paths = set(data_rows.keys())
	for path in list(all_paths):
		parts = path.strip('/').split('/')
		for j in range(1, len(parts)):
			prefix = '/' + '/'.join(parts[:j])
			if prefix not in data_rows:
				data_rows[prefix] = {h: '' for h in in_headers}
				data_rows[prefix]['path'] = prefix
				all_paths.add(prefix)

	if not all_paths:
		return

	# Read filter text from toolbar textCOMP (or legacy widgetCOMP)
	filter_op = parent.Embody.op('toolbar/container_right/new_filter') or parent.Embody.op('toolbar/container_right/filter')
	filter_text = ''
	if filter_op:
		if hasattr(filter_op.par, 'Value0'):
			filter_text = filter_op.par.Value0.eval().strip().lower()
		else:
			filter_text = filter_op.par.text.eval().strip().lower()

	# Read filter toggles from the Embody COMP (Phase 4 params).
	# Both default to "show everything" so older .toes without these
	# params still work via getattr fallback.
	filter_dirty = bool(getattr(parent.Embody.par, 'Filterdirty', None)
						and parent.Embody.par.Filterdirty.eval())
	filter_dats = bool(getattr(parent.Embody.par, 'Filterdats', None)
						and parent.Embody.par.Filterdats.eval())

	# Tree vs flat: in flat mode the synthetic-parent rows that were
	# injected above are removed before sorting / writing. Each real op
	# stands on its own row at depth 0.
	listmode_par = getattr(parent.Embody.par, 'Listmode', None)
	flat_mode = (listmode_par.eval() == 'flat') if listmode_par else False
	if flat_mode:
		# Keep only paths that came from the input table (have a real op or
		# at least a non-empty type/rel_file_path); drop pure synthetic
		# ancestors injected above.
		real_paths = {
			path for path in all_paths
			if data_rows[path].get('rel_file_path', '')
			or data_rows[path].get('type', '')
		}
		all_paths = real_paths
		if not all_paths:
			return

	def _row_is_dirty(row):
		val = str(row.get('dirty', '')).strip()
		return val not in ('', 'False', 'false', '0', 'Clean', 'Saved')

	def _row_is_dat(path, row):
		"""True if the row is a DAT externalization (not a synthetic parent
		and not a COMP)."""
		oper = op(path)
		if oper is None:
			# Synthetic parent -- not itself a DAT
			return False
		return oper.family == 'DAT'

	# Apply text + state filters (case-insensitive substring match against
	# path and file path; isDirty + hide-DATs gating).
	if filter_text or filter_dirty or filter_dats:
		matched_paths = set()
		for path in all_paths:
			row = data_rows[path]
			# Only filter against ops that have a row of their own --
			# synthetic parents come along for the ride below.
			if op(path) is None:
				continue
			if filter_text:
				searchable = (path + ' ' + row.get('rel_file_path', '')).lower()
				if filter_text not in searchable:
					continue
			if filter_dirty and not _row_is_dirty(row):
				continue
			if filter_dats and _row_is_dat(path, row):
				continue
			matched_paths.add(path)

		# Tree mode: include ancestor paths so hierarchy stays readable
		# (a dirty leaf without its parent chain shown is disorienting).
		# Flat (list) mode: skip ancestor injection -- the user explicitly
		# opted out of hierarchy, so the filter should be strict.  Without
		# this, filtering by dirty in flat mode shows clean COMPs that
		# merely happen to be ancestors of a dirty descendant, which looks
		# like a bug ("why is this clean COMP in my dirty list?").
		if flat_mode:
			all_paths = matched_paths
		else:
			paths_to_keep = set()
			for path in matched_paths:
				paths_to_keep.add(path)
				parts = path.strip('/').split('/')
				for j in range(1, len(parts)):
					ancestor = '/' + '/'.join(parts[:j])
					if ancestor in all_paths:
						paths_to_keep.add(ancestor)
			all_paths = paths_to_keep
		if not all_paths:
			return

	# Detect active TDN export
	exporting_path = None
	tdn_ext = getattr(parent.Embody.ext, 'TDN', None)
	export_state = getattr(tdn_ext, '_export_state', None) if tdn_ext else None
	if export_state and not export_state.get('done'):
		exporting_path = export_state.get('root_path')

	# Compute depth relative to shallowest path
	min_depth = min(p.count('/') for p in all_paths)

	# Compute has_children by marking each path's parent
	has_children = set()
	for path in all_paths:
		parts = path.strip('/').split('/')
		if len(parts) > 1:
			parent_p = '/' + '/'.join(parts[:-1])
			if parent_p in all_paths:
				has_children.add(parent_p)

	# Get expand/collapse state
	expanded = parent.Embody.fetch('expanded_paths', None)
	if expanded is None:
		# Start with root-level items expanded
		roots = set()
		for path in all_paths:
			parts = path.strip('/').split('/')
			parent_p = '/' + '/'.join(parts[:-1]) if len(parts) > 1 else None
			if parent_p is None or parent_p not in all_paths:
				roots.add(path)
		expanded = roots & has_children
		parent.Embody.store('expanded_paths', expanded)

	# LRU tracking for row limit enforcement
	expand_order = parent.Embody.fetch('expand_order', None)
	if expand_order is None:
		expand_order = list(expanded)  # seed from existing set if upgrading
		parent.Embody.store('expand_order', expand_order)

	# Sort and filter by visibility
	sorted_paths = sorted(all_paths)
	visible_expanded = set()
	visible = []

	for path in sorted_paths:
		parts = path.strip('/').split('/')
		parent_path = '/' + '/'.join(parts[:-1]) if len(parts) > 1 else None
		if (parent_path is None
				or parent_path not in all_paths
				or parent_path in visible_expanded):
			visible.append(path)
			if path in has_children and path in expanded:
				visible_expanded.add(path)

	# Clean stale paths from LRU tracker (mutate in-place to keep reference)
	expand_order[:] = [p for p in expand_order if p in all_paths]

	# Enforce row limit -- collapse the node with the fewest visible
	# children first, but never collapse the active branch
	active = expand_order[-1] if expand_order else None
	protected = set()
	if active:
		parts = active.strip('/').split('/')
		for i in range(1, len(parts) + 1):
			protected.add('/' + '/'.join(parts[:i]))

	def _child_count(p):
		"""Count visible rows that are direct/indirect children of p."""
		prefix = p + '/'
		return sum(1 for v in visible if v.startswith(prefix))

	while len(visible) > MAX_VISIBLE_ROWS:
		# Candidates: any expanded node not in the active branch
		candidates = [p for p in expanded if p not in protected
		              and p in has_children]
		if not candidates:
			break  # nothing left to collapse without breaking active branch
		# Collapse the candidate with the fewest visible children
		smallest = min(candidates, key=_child_count)
		if smallest in expand_order:
			expand_order.remove(smallest)
		expanded.discard(smallest)
		# Rebuild visible list
		visible_expanded = set()
		visible = []
		for path in sorted_paths:
			parts = path.strip('/').split('/')
			parent_path = '/' + '/'.join(parts[:-1]) if len(parts) > 1 else None
			if (parent_path is None
					or parent_path not in all_paths
					or parent_path in visible_expanded):
				visible.append(path)
				if path in has_children and path in expanded:
					visible_expanded.add(path)

	# NOTE: expanded and expand_order are mutated in-place above.
	# Do NOT call store() here -- it triggers recooks and would cause
	# an infinite cook loop since this DAT fetches from the same keys.

	# Apply column sort if one is active.  Sort state is stored on the
	# Embody COMP as {'col': <field>, 'dir': 1|-1}.  Sorting flattens the
	# tree visually (children no longer cluster under their parent) --
	# users who want hierarchy keep the default tree order.
	sort_state = parent.Embody.fetch('sort_state', None, search=False)
	if sort_state:
		field = sort_state.get('col')
		direction = sort_state.get('dir', 1)
		_ROW_STATE_RANK = {
			'Dirty': 0, 'ParChange': 1, 'Exporting': 2,
			'Saved': 3, 'Comp': 4, '': 5,
		}

		def _sort_key(p):
			row = data_rows.get(p, {})
			if field == 'path':
				return (p.rsplit('/', 1)[-1] or p).lower()
			if field == 'rel_file_path':
				return row.get('rel_file_path', '').lower()
			if field == 'type':
				return row.get('type', '').lower()
			if field == 'row_state':
				# Resolve the same state derivation as the writer below.
				rel = row.get('rel_file_path', '')
				oper = op(p)
				is_comp = oper and oper.family == 'COMP'
				if not rel and not is_comp:
					st = ''
				elif is_comp and not rel:
					st = 'Comp'
				elif is_comp and p == exporting_path:
					st = 'Exporting'
				elif row.get('dirty', '') == 'Par':
					st = 'ParChange'
				elif row.get('dirty', '') in ('True', 'true', '1'):
					st = 'Dirty'
				else:
					st = 'Saved'
				return _ROW_STATE_RANK.get(st, 6)
			return p.lower()

		visible = sorted(visible, key=_sort_key,
		                 reverse=(direction == -1))

	# Write output
	out_headers = ['path', 'type', 'rel_file_path', 'timestamp',
	               'build', 'touch_build', 'row_state',
	               'depth', 'has_children']
	scriptOp.appendRow(out_headers)

	for path in visible:
		row = data_rows[path]
		# In flat mode every row is depth=0 with no expand markers; the tree
		# logic above still ran but we collapse the visible shape here.
		if flat_mode:
			depth = 0
			hc = '0'
		else:
			depth = path.count('/') - min_depth
			hc = '1' if path in has_children else '0'

		oper = op(path)
		is_comp = oper and oper.family == 'COMP'
		rel = row.get('rel_file_path', '')
		dirty_val = row.get('dirty', '')

		# Compute row state. Every externalized op (COMP or DAT) shares the
		# same state machine -- no per-strategy split.
		if not rel and not is_comp:
			# Synthetic parent or unexternalized op
			row_state = ''
		elif is_comp and not rel:
			# Unexternalized COMP shown only as a tree parent
			row_state = 'Comp'
		elif is_comp and path == exporting_path:
			row_state = 'Exporting'
		elif dirty_val == 'Par':
			row_state = 'ParChange'
		elif dirty_val in ('True', 'true', '1'):
			row_state = 'Dirty'
		else:
			row_state = 'Saved'

		scriptOp.appendRow([
			path,
			row.get('type', ''),
			rel,
			row.get('timestamp', ''),
			row.get('build', ''),
			row.get('touch_build', ''),
			row_state,
			str(depth),
			hc,
		])
