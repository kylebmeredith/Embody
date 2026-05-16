"""
Test suite: MCP externalization integration handlers in EnvoyExt.

Tests _externalize_op (par-driven, rel_path arg), _remove_externalization
(renamed from _remove_externalization_tag in Phase 5b),
_get_externalizations, _get_externalization_status.

The tag-type auto-detection tests from before Phase 5b are gone --
the API now takes an optional rel_path and infers the file extension
from dat_type_to_extension. There's no tag system anymore.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPExternalization(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.envoy = self.embody.ext.Envoy

	def tearDown(self):
		"""Clear par.externaltox / par.file on sandbox ops + drop their
		rows from the table."""
		for child in self.sandbox.findChildren():
			try:
				if child.family == 'COMP' and child.par.externaltox.eval():
					child.par.externaltox.readOnly = False
					child.par.externaltox = ''
				elif child.family == 'DAT' and child.par.file.eval():
					child.par.file.readOnly = False
					child.par.file = ''
					child.par.syncfile = False
			except Exception:
				pass
		for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
			path = self.embody_ext.Externalizations[i, 'path'].val
			if path.startswith(self.sandbox.path):
				self.embody_ext.Externalizations.deleteRow(i)
		super().tearDown()

	# --- _get_externalizations ---

	def test_get_externalizations_returns_list(self):
		result = self.envoy._get_externalizations()
		self.assertDictHasKey(result, 'externalizations')
		self.assertIsInstance(result['externalizations'], list)

	def test_get_externalizations_has_entries(self):
		result = self.envoy._get_externalizations()
		self.assertGreater(len(result['externalizations']), 0)

	def test_get_externalizations_entry_structure(self):
		result = self.envoy._get_externalizations()
		if result['externalizations']:
			entry = result['externalizations'][0]
			self.assertDictHasKey(entry, 'path')
			self.assertDictHasKey(entry, 'type')

	# --- _get_externalization_status ---

	def test_get_externalization_status_nonexistent(self):
		result = self.envoy._get_externalization_status(
			op_path='/nonexistent')
		self.assertDictHasKey(result, 'error')

	# --- _externalize_op ---

	def test_externalize_op_comp_default_path(self):
		"""COMP: no rel_path -> auto-derived {folder}/{name}.tox."""
		comp = self.sandbox.create(baseCOMP, 'mcp_ext_comp')
		result = self.envoy._externalize_op(op_path=comp.path)
		self.assertTrue(result.get('success'))
		self.assertEndsWith(result['file'], 'mcp_ext_comp.tox')

	def test_externalize_op_comp_explicit_path(self):
		"""COMP: explicit rel_path is honored."""
		comp = self.sandbox.create(baseCOMP, 'mcp_ext_explicit')
		result = self.envoy._externalize_op(
			op_path=comp.path, rel_path='custom/place.tox')
		self.assertTrue(result.get('success'))
		self.assertEqual(result['file'], 'custom/place.tox')

	def test_externalize_op_textdat_default_extension(self):
		"""textDAT: extension derived from dat_type_to_extension (py)."""
		dat = self.sandbox.create(textDAT, 'mcp_ext_dat')
		result = self.envoy._externalize_op(op_path=dat.path)
		self.assertTrue(result.get('success'))
		self.assertEndsWith(result['file'], 'mcp_ext_dat.py')

	def test_externalize_op_tabledat_default_extension(self):
		"""tableDAT: extension is .tsv per dat_type_to_extension."""
		dat = self.sandbox.create(tableDAT, 'mcp_ext_table')
		result = self.envoy._externalize_op(op_path=dat.path)
		self.assertTrue(result.get('success'))
		self.assertEndsWith(result['file'], 'mcp_ext_table.tsv')

	def test_externalize_op_unsupported_dat_type_errors(self):
		"""DAT type not in supported_dat_types -> error."""
		dat = self.sandbox.create(infoDAT, 'mcp_ext_info')
		result = self.envoy._externalize_op(op_path=dat.path)
		self.assertDictHasKey(result, 'error')

	def test_externalize_op_nonexistent(self):
		result = self.envoy._externalize_op(op_path='/nonexistent')
		self.assertDictHasKey(result, 'error')

	# --- _remove_externalization ---

	def test_remove_externalization_clears_comp_par(self):
		comp = self.sandbox.create(baseCOMP, 'rm_ext_comp')
		self.envoy._externalize_op(op_path=comp.path)
		self.assertTrue(bool(comp.par.externaltox.eval()))
		result = self.envoy._remove_externalization(op_path=comp.path)
		self.assertTrue(result.get('success'))
		self.assertFalse(bool(comp.par.externaltox.eval()))

	def test_remove_externalization_clears_dat_par(self):
		dat = self.sandbox.create(textDAT, 'rm_ext_dat')
		self.envoy._externalize_op(op_path=dat.path)
		self.assertTrue(bool(dat.par.file.eval()))
		result = self.envoy._remove_externalization(op_path=dat.path)
		self.assertTrue(result.get('success'))
		self.assertFalse(bool(dat.par.file.eval()))

	def test_remove_externalization_nonexistent(self):
		result = self.envoy._remove_externalization(op_path='/nonexistent')
		self.assertDictHasKey(result, 'error')
