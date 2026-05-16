"""
Test suite: DAT restoration on startup and continuity check hardening.

Tests RestoreDATs(), _getDATEntries(), and the file-existence guard in
checkOpsForContinuity() that protects recoverable entries from deletion.
"""

from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDATRestoration(EmbodyTestCase):

    def setUp(self):
        self._test_dir = Path(project.folder) / 'embody' / 'unit_tests' / '_test_temp'
        self._test_dir.mkdir(parents=True, exist_ok=True)
        # Create a root-level sandbox outside all TDN/TOX COMPs.
        # The normal sandbox is inside /embody/unit_tests (a TDN COMP),
        # so _getDATEntries() correctly filters it. Restoration tests
        # need a parent that's NOT inside any managed COMP.
        self._root_sandbox = op('/').create(baseCOMP, '_test_dat_restore')
        # Track table rows we add so tearDown can clean them up
        self._added_paths = []

    def tearDown(self):
        # Clean up root-level sandbox
        if op(self._root_sandbox.path):
            self._root_sandbox.destroy()
        # Clean up any table entries we injected
        table = self.embody_ext.Externalizations
        for path in self._added_paths:
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val == path:
                    table.deleteRow(i)
        # Clean up temp files
        for f in self._test_dir.glob('*'):
            try:
                f.unlink()
            except OSError:
                pass
        super().tearDown()

    # --- Helpers ---

    def _add_table_entry(self, path, dat_type, strategy, rel_file_path):
        """Add a row to the externalizations table directly."""
        table = self.embody_ext.Externalizations
        table.appendRow([
            path, dat_type, strategy, rel_file_path,
            '2026-01-01 00:00:00 UTC', '', '', '',
            '0', '0', ''
        ])
        self._added_paths.append(path)

    def _create_file(self, rel_path, content='# test'):
        """Create a file on disk at the given relative path."""
        abs_path = self.embody_ext.buildAbsolutePath(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding='utf-8')
        return abs_path

    def _table_has_path(self, path):
        """Check if the externalizations table has an entry for the given path."""
        table = self.embody_ext.Externalizations
        for i in range(1, table.numRows):
            if table[i, 'path'].val == path:
                return True
        return False

    # =================================================================
    # RestoreDATs — basic restoration
    # =================================================================

    def test_restore_missing_text_dat(self):
        """Missing textDAT with .py file on disk should be recreated."""
        dat_path = self._root_sandbox.path + '/restored_text'
        rel_path = 'embody/unit_tests/_test_temp/restored_text.py'
        self._create_file(rel_path, '# restored content')
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'textDAT should be restored')
        self.assertEqual(restored.type, 'text')
        self.assertIn('restored_text.py', restored.par.file.eval())
        self.assertTrue(restored.par.syncfile.eval())

    def test_restore_missing_table_dat(self):
        """Missing tableDAT with .tsv file on disk should be recreated."""
        dat_path = self._root_sandbox.path + '/restored_table'
        rel_path = 'embody/unit_tests/_test_temp/restored_table.tsv'
        self._create_file(rel_path, 'col1\tcol2\nval1\tval2')
        self._add_table_entry(dat_path, 'table', 'tsv', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'tableDAT should be restored')
        self.assertEqual(restored.type, 'table')

    # Phase 5b removed RestoreDATs's tag/color reapply step -- restored
    # DATs no longer get an Embody tag added or their color tinted. Both
    # tests below would assert on behavior that doesn't exist anymore;
    # left here as a marker for the missing coverage. If a future phase
    # re-introduces visual indicators for externalized DATs, restore the
    # assertions then.

    # =================================================================
    # RestoreDATs — skip conditions
    # =================================================================

    def test_restore_skips_existing_dat(self):
        """DAT that already exists should not be recreated."""
        existing = self._root_sandbox.create(textDAT, 'existing_dat')
        rel_path = 'embody/unit_tests/_test_temp/existing_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(existing.path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()
        self.assertIs(op(existing.path), existing)

    def test_restore_skips_missing_file(self):
        """Entry with no file on disk should be skipped (not crash)."""
        dat_path = self._root_sandbox.path + '/no_file_dat'
        rel_path = 'embody/unit_tests/_test_temp/no_file_dat.py'
        # Do NOT create the file
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        self.assertIsNone(op(dat_path), 'DAT should not be created without file')

    def test_restore_skips_embody_descendants(self):
        """DATs inside Embody's own path should never be restored."""
        embody_path = self.embody.path
        dat_path = embody_path + '/internal_dat'
        rel_path = 'embody/unit_tests/_test_temp/internal_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths, 'Embody descendants should be excluded')

    def test_restore_skips_dat_inside_tox_comp(self):
        """DATs inside a TOX-strategy COMP should be excluded."""
        comp_path = self._root_sandbox.path + '/tox_parent'
        self._add_table_entry(comp_path, 'container', 'tox',
                              'embody/unit_tests/_test_temp/tox_parent.tox')

        dat_path = comp_path + '/child_dat'
        rel_path = 'embody/unit_tests/_test_temp/child_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths,
                         'DATs inside TOX COMPs should be excluded')

    def test_restore_skips_dat_inside_tdn_comp(self):
        """DATs inside a TDN-strategy COMP should be excluded."""
        comp_path = self._root_sandbox.path + '/tdn_parent'
        self._add_table_entry(comp_path, 'container', 'tdn',
                              'embody/unit_tests/_test_temp/tdn_parent.tdn')

        dat_path = comp_path + '/child_dat'
        rel_path = 'embody/unit_tests/_test_temp/child_dat2.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths,
                         'DATs inside TDN COMPs should be excluded')

    def test_restore_skips_when_parent_missing(self):
        """DAT whose parent doesn't exist should be skipped gracefully."""
        dat_path = '/nonexistent_parent/orphan_dat'
        rel_path = 'embody/unit_tests/_test_temp/orphan_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        # Should not raise
        self.embody_ext.RestoreDATs()
        self.assertIsNone(op(dat_path))

    def test_restore_disabled_by_toggle(self):
        """RestoreDATs should be a no-op when Datrestoreonstart is off."""
        dat_path = self._root_sandbox.path + '/toggle_off_dat'
        rel_path = 'embody/unit_tests/_test_temp/toggle_off_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        orig = self.embody.par.Datrestoreonstart.eval()
        self.embody.par.Datrestoreonstart = False
        try:
            self.embody_ext.RestoreDATs()
            self.assertIsNone(op(dat_path),
                              'DAT should not be restored when toggle is off')
        finally:
            self.embody.par.Datrestoreonstart = orig

    # =================================================================
    # Continuity check hardening
    # =================================================================
    #
    # The continuity machinery was removed in Phase 5a: TD natively
    # preserves par.externaltox / par.file across renames, and the
    # externalizations table is now rebuilt from a live par scan via
    # _scanAndPopulate so stale "recoverable" entries no longer exist
    # as a category. The three tests that exercised checkOpsForContinuity
    # were dropped along with that machinery.
