"""
Test suite: Deletion and cleanup -- RemoveListerRow + file references.

Pre-fork tests for _handleMissingOperator + tag/color teardown are gone:
- _handleMissingOperator was part of the continuity machinery removed
  in Phase 5a.
- Tag removal + resetOpColor were removed in Phase 5b (no Embody-defined
  tags anymore).

Surviving behavior under test:
- RemoveListerRow clears par.externaltox / par.file
- RemoveListerRow handles destroyed-op + nonexistent-path cases
- _checkFileReferences detects shared external paths
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestDeleteCleanup(EmbodyTestCase):

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        for child in self.sandbox.findChildren():
            try:
                if child.family == 'COMP' and hasattr(child.par, 'externaltox'):
                    child.par.externaltox.readOnly = False
                    child.par.externaltox = ''
                elif child.family == 'DAT' and hasattr(child.par, 'file'):
                    child.par.file.readOnly = False
                    child.par.file = ''
            except Exception:
                pass
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # --- Helpers ---

    def _externalize_comp(self, parent, name):
        comp = parent.create(baseCOMP, name)
        comp.par.externaltox = f'embody/unit_tests/_test_temp/{name}.tox'
        self.embody_ext.handleAddition(comp)
        return comp, comp.path, self.embody_ext.normalizePath(
            comp.par.externaltox.eval())

    def _externalize_dat(self, parent, name):
        dat = parent.create(textDAT, name)
        dat.par.file = f'embody/unit_tests/_test_temp/{name}.py'
        self.embody_ext.handleAddition(dat)
        return dat, dat.path, self.embody_ext.normalizePath(
            dat.par.file.eval())

    # =========================================================================
    # RemoveListerRow -- COMP
    # =========================================================================

    def test_removeListerRow_comp_clears_externaltox(self):
        comp, old_path, old_rel = self._externalize_comp(
            self.workspace, 'rm_ext')
        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertEqual(comp.par.externaltox.eval(), '')

    def test_removeListerRow_comp_unlocks_readonly(self):
        comp, old_path, old_rel = self._externalize_comp(
            self.workspace, 'rm_ro')
        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertFalse(comp.par.externaltox.readOnly)

    # =========================================================================
    # RemoveListerRow -- DAT
    # =========================================================================

    def test_removeListerRow_dat_clears_file(self):
        dat, old_path, old_rel = self._externalize_dat(
            self.workspace, 'rm_dat_file')
        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertEqual(dat.par.file.eval(), '')

    # =========================================================================
    # Edge cases
    # =========================================================================

    def test_removeListerRow_destroyed_op_no_crash(self):
        """RemoveListerRow on an already-destroyed op must not raise."""
        comp, old_path, old_rel = self._externalize_comp(
            self.workspace, 'rm_destroyed')
        comp.destroy()
        # Should not raise
        self.embody_ext.RemoveListerRow(old_path, old_rel)

    def test_removeListerRow_nonexistent_path_no_crash(self):
        """RemoveListerRow with a path not in the table must not raise."""
        self.embody_ext.RemoveListerRow('/nonexistent/path', 'fake/file.tox')

    # =========================================================================
    # _checkFileReferences
    # =========================================================================

    def test_checkFileReferences_unique_file_returns_false(self):
        comp, old_path, old_rel = self._externalize_comp(
            self.workspace, 'unique_file')
        shared = self.embody_ext._checkFileReferences(old_path, old_rel)
        self.assertFalse(shared)

    def test_checkFileReferences_shared_file_returns_true(self):
        comp1, _, old_rel = self._externalize_comp(
            self.workspace, 'shared1')
        comp2 = self.workspace.create(baseCOMP, 'shared2')
        comp2.par.externaltox = comp1.par.externaltox.eval()
        shared = self.embody_ext._checkFileReferences(comp1.path, old_rel)
        self.assertTrue(shared)
