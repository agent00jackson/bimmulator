"""Exercise profile-to-exec composition without starting QEMU or firmware."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAIN = runpy.run_path(str(ROOT / 'tools/run-hmi-profile.py'))['main']


class ProfileSocketTests(unittest.TestCase):
    def compose(self, platform, component, override=None):
        with tempfile.TemporaryDirectory(prefix='id6-qmp-', dir='/tmp') as directory:
            base = Path(directory).resolve()
            source = base / 'rootfs'
            source.mkdir()
            cache = base / component
            profile = base / 'profile.json'
            profile.write_text(json.dumps({
                'schema': 'idrive6-hmi-launch-profile-v1', 'name': 'socket-test',
                'settings': {}, 'paths': {
                    'IDRIVE_ROOTFS_TREE': str(source),
                    'IDRIVE_QEMU_QMP_SOCKET': '{run_dir}/qmp.sock',
                },
            }))
            args = ['run-hmi-profile.py', str(profile), '--run-id', 'socket-test']
            if override is not None:
                args += ['--set', 'IDRIVE_QEMU_QMP_SOCKET=' + override]
            with patch.object(sys, 'argv', args), patch.object(sys, 'platform', platform), \
                    patch.dict(os.environ, {'IDRIVE_CACHE_DIR': str(cache)}), \
                    patch('os.execvpe') as execute:
                self.assertEqual(MAIN(), 127)
            return execute.call_args.args[2]['IDRIVE_QEMU_QMP_SOCKET'], cache / 'runs/socket-test/qmp.sock'

    def test_long_generated_linux_and_darwin_paths_are_shortened(self):
        for platform, limit in (('linux', 108), ('darwin', 104)):
            with self.subTest(platform=platform):
                selected, original = self.compose(platform, 'a' * 90)
                self.assertGreaterEqual(len(os.fsencode(original)), limit)
                self.assertTrue(selected.startswith('/tmp/idrive6-qmp-'))
                self.assertLess(len(os.fsencode(selected)), limit)

    def test_linux_limit_counts_encoded_bytes_not_characters(self):
        selected, original = self.compose('linux', 'é' * 34)
        self.assertLess(len(str(original)), 108)
        self.assertGreaterEqual(len(os.fsencode(original)), 108)
        self.assertNotEqual(selected, str(original))

    def test_short_generated_linux_path_is_preserved(self):
        selected, original = self.compose('linux', 'short')
        self.assertEqual(selected, str(original))

    def test_explicit_socket_override_is_not_silently_replaced(self):
        override = '/chosen/' + 'q' * 120 + '.sock'
        selected, _ = self.compose('linux', 'a' * 90, override)
        self.assertEqual(selected, override)
