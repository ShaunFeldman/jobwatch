import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'work'
        self.repo.mkdir()
        self.run_cmd('git', 'init', '--bare', str(self.base / 'remote'))
        self.run_cmd('git', 'init')
        self.run_cmd('git', 'config', 'user.name', 'test')
        self.run_cmd('git', 'config', 'user.email', 'test@example.com')
        self.run_cmd('git', 'remote', 'add', 'origin', str(self.base / 'remote'))
        (self.repo / 'source.txt').write_text('source')
        self.run_cmd('git', 'add', 'source.txt')
        self.run_cmd('git', 'commit', '-m', 'source')
        self.state = {'version': 5, 'boards': {}, 'alerted': [['key', 'time']],
                      'pending': [['queued']], 'outbox': [], 'kv': {'proof': 'preserved'}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *args, check=True):
        env = dict(os.environ)
        env.pop('GH_TOKEN', None)
        env.pop('GITHUB_REPOSITORY', None)
        return subprocess.run(args, cwd=self.repo, env=env, capture_output=True, check=check)

    def save(self):
        (self.repo / 'state.json').write_text(json.dumps(self.state))
        self.run_cmd('bash', str(ROOT / '.github/scripts/save-state.sh'))

    def test_compressed_roundtrip_preserves_queues_and_source_checkout(self):
        head = self.run_cmd('git', 'rev-parse', 'HEAD').stdout
        (self.repo / 'staged.txt').write_text('unrelated staged work')
        self.run_cmd('git', 'add', 'staged.txt')
        index = self.run_cmd('git', 'diff', '--cached').stdout
        self.save()
        self.assertEqual(head, self.run_cmd('git', 'rev-parse', 'HEAD').stdout)
        self.assertEqual(index, self.run_cmd('git', 'diff', '--cached').stdout)
        packed = self.run_cmd('git', 'show', 'refs/remotes/origin/state:state.json.gz').stdout
        self.assertEqual(self.state, json.loads(gzip.decompress(packed)))
        (self.repo / 'state.json').unlink()
        self.run_cmd('python3', str(ROOT / '.github/scripts/restore-state.py'))
        self.assertEqual(self.state, json.loads((self.repo / 'state.json').read_text()))
        self.save()
        self.assertEqual(b'1', self.run_cmd('git', 'rev-list', '--count', 'refs/remotes/origin/state').stdout.strip())

    def test_legacy_uncompressed_checkpoint_restores(self):
        (self.repo / 'state.json').write_text(json.dumps(self.state))
        self.run_cmd('git', 'add', 'state.json')
        self.run_cmd('git', 'commit', '-m', 'legacy checkpoint')
        self.run_cmd('git', 'push', 'origin', 'HEAD:state')
        (self.repo / 'state.json').unlink()
        self.run_cmd('python3', str(ROOT / '.github/scripts/restore-state.py'))
        self.assertEqual(self.state, json.loads((self.repo / 'state.json').read_text()))

    def test_invalid_json_cannot_replace_a_valid_checkpoint(self):
        self.save()
        before = self.run_cmd('git', 'ls-remote', 'origin', 'refs/heads/state').stdout
        (self.repo / 'state.json').write_text('broken')
        result = self.run_cmd('bash', str(ROOT / '.github/scripts/save-state.sh'), check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.run_cmd('git', 'ls-remote', 'origin', 'refs/heads/state').stdout)
