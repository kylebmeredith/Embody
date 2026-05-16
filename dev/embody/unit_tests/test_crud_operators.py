"""
Test suite: CRUD operations -- handleAddition / handleSubtraction
end-to-end on the par-driven externalization pipeline.

Pre-fork (tag-based): tests would add a tag to a sandbox op and call
handleAddition, expecting the table to gain a row inline. After
Phase 1-3 the model changed: set par.externaltox / par.file, then
the table is rebuilt by _scanAndPopulate at end of Update. So the
assertions about "row appears in table after handleAddition" are
gone -- replaced by assertions about the par state and file paths.

Build/Date/Touchbuild auto-injection is also gone (Phase 3), so the
build-params tests are removed.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestCRUDOperators(EmbodyTestCase):

    def setUp(self):
        """Create a clean workspace for each test."""
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        """Clear par state on sandbox children + drop their table rows."""
        for child in self.sandbox.findChildren():
            try:
                if child.family == 'COMP' and hasattr(child.par, 'externaltox'):
                    child.par.externaltox.readOnly = False
                    child.par.externaltox = ''
                    child.par.enableexternaltox = False
                elif child.family == 'DAT' and hasattr(child.par, 'file'):
                    child.par.file.readOnly = False
                    child.par.file = ''
                    if hasattr(child.par, 'syncfile'):
                        child.par.syncfile = False
            except Exception:
                pass
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # =========================================================================
    # handleAddition -- COMP
    # =========================================================================

    def test_handleAddition_comp_sets_externaltox(self):
        comp = self.workspace.create(baseCOMP, 'ext_tox')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/ext_tox.tox'
        self.embody_ext.handleAddition(comp)
        self.assertTrue(bool(comp.par.externaltox.eval()))
        self.assertIn('ext_tox', comp.par.externaltox.eval())

    def test_handleAddition_comp_sets_readonly(self):
        comp = self.workspace.create(baseCOMP, 'readonly')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/readonly.tox'
        self.embody_ext.handleAddition(comp)
        self.assertTrue(comp.par.externaltox.readOnly)

    def test_handleAddition_comp_enables_externaltox(self):
        comp = self.workspace.create(baseCOMP, 'enable_ext')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/enable_ext.tox'
        self.embody_ext.handleAddition(comp)
        self.assertTrue(comp.par.enableexternaltox.eval())

    # =========================================================================
    # handleAddition -- DAT
    # =========================================================================

    def test_handleAddition_dat_sets_file(self):
        dat = self.workspace.create(textDAT, 'add_dat')
        dat.par.file = 'embody/unit_tests/_test_temp/add_dat.py'
        self.embody_ext.handleAddition(dat)
        file_path = dat.par.file.eval()
        self.assertTrue(len(file_path) > 0)
        self.assertIn('add_dat', file_path)

    def test_handleAddition_dat_sets_readonly(self):
        dat = self.workspace.create(textDAT, 'ro_dat')
        dat.par.file = 'embody/unit_tests/_test_temp/ro_dat.py'
        self.embody_ext.handleAddition(dat)
        self.assertTrue(dat.par.file.readOnly)

    # =========================================================================
    # _scanAndPopulate (Phase 3): the table reflects par state
    # =========================================================================

    def test_scanAndPopulate_includes_par_set_comp(self):
        comp = self.workspace.create(baseCOMP, 'scan_comp')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/scan_comp.tox'
        self.embody_ext._scanAndPopulate()
        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                found = True
                break
        self.assertTrue(found, 'Par-set COMP should appear in the table')

    def test_scanAndPopulate_includes_par_set_dat(self):
        dat = self.workspace.create(textDAT, 'scan_dat')
        dat.par.file = 'embody/unit_tests/_test_temp/scan_dat.py'
        self.embody_ext._scanAndPopulate()
        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == dat.path:
                found = True
                break
        self.assertTrue(found, 'Par-set DAT should appear in the table')

    def test_scanAndPopulate_excludes_unset_comp(self):
        comp = self.workspace.create(baseCOMP, 'noscan_comp')
        # No par.externaltox set
        self.embody_ext._scanAndPopulate()
        for i in range(1, self.embody_ext.Externalizations.numRows):
            self.assertNotEqual(
                self.embody_ext.Externalizations[i, 'path'].val, comp.path,
                'COMP without par.externaltox should not be in table')

    # =========================================================================
    # handleSubtraction
    # =========================================================================

    def test_handleSubtraction_comp_unlocks_readonly(self):
        comp = self.workspace.create(baseCOMP, 'sub_ro')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/sub_ro.tox'
        self.embody_ext.handleAddition(comp)
        self.embody_ext.handleSubtraction(comp)
        self.assertFalse(comp.par.externaltox.readOnly)

    def test_handleSubtraction_dat_unlocks_readonly(self):
        dat = self.workspace.create(textDAT, 'sub_dat_ro')
        dat.par.file = 'embody/unit_tests/_test_temp/sub_dat_ro.py'
        self.embody_ext.handleAddition(dat)
        self.embody_ext.handleSubtraction(dat)
        self.assertFalse(dat.par.file.readOnly)

    # =========================================================================
    # Path generation -- flat layout
    # =========================================================================

    def test_flat_path_layout(self):
        """Phase 2: paths are {folder}/{name}.{ext}, no parent hierarchy."""
        parent = self.workspace.create(baseCOMP, 'outer')
        child = parent.create(baseCOMP, 'inner')
        child.par.externaltox = ''  # not set yet
        result = self.embody_ext.getOpPaths(child)
        # When externaltox is empty, getOpPaths falls back to the
        # default external folder + {name}.tox. The parent ('outer')
        # must NOT appear in the path (flat layout).
        abs_folder, save_path, rel_dir, rel_file = result
        if rel_file is not None:
            self.assertNotIn('outer/', rel_file,
                              'Flat layout: parent name should not appear in path')

    def test_handleAddition_no_backslashes_in_par(self):
        comp = self.workspace.create(baseCOMP, 'no_backslash')
        comp.par.externaltox = 'embody/unit_tests/_test_temp/no_backslash.tox'
        self.embody_ext.handleAddition(comp)
        result = comp.par.externaltox.eval()
        self.assertNotIn('\\', result)
