"""Freshness contracts against isolated real Git workspaces; no workers/services."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib'))
import core
from handoff import render_handoff

CLI = runpy.run_path(str(REPO / 'bin/cs'), run_name='cs_freshness_cli')


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.cwd = self.base / 'repo'
        self.cwd.mkdir()
        self.root = self.base / 'state'
        env = patch.dict(os.environ, {'CS_HOME': str(self.root), 'CS_JOB_ID': ''})
        env.start()
        self.addCleanup(env.stop)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.test')
        self.git('config', 'user.name', 'Fixture')
        (self.cwd / 'notes.md').write_text('one\ntwo\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.cwd), *args], text=True).strip()

    def cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['cs', *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI['main'](), 0, output.getvalue())
        return json.loads(output.getvalue())

    def submit(self, *extra):
        self.cli('submit', '--id', 'original', '--project', 'fixture', '--area', 'backend',
                 '--tier', 'M', '--cwd', str(self.cwd), '--files', 'notes.md', '--prompt-text', 'fix',
                 '--context', 'notes.md:1-1', '--no-tick', '--json', *extra)
        return core.find_job(self.root, 'original')

    def terminal(self, job):
        core.remove_job(self.root, 'queue', job['id'])
        job.update(status='failed', pool='codex', codex_session_id='session-fixture')
        core.write_job(self.root, 'done', job)

    def test_capture_metadata_and_fresh_card(self):
        job = self.submit()
        snapshot = job['context_snapshot']
        self.assertEqual(snapshot['base_sha'], self.git('rev-parse', 'HEAD'))
        self.assertTrue(snapshot['captured_at'])
        self.assertEqual(snapshot['paths'], [str(self.cwd / 'notes.md')])
        self.assertEqual(job['context_text'], '### notes.md:1-1\none\n')
        summary = core.job_summary(job)
        self.assertEqual(summary['freshness'], 'fresh')
        self.assertEqual(summary['handoff']['freshness'], 'fresh')
        self.assertIn('freshness: fresh', render_handoff(summary['handoff'], job['id']))
        self.assertNotIn('resume_session_id', job)

    def test_head_change_warns_on_explicit_retry_preserving_snapshot(self):
        original = self.submit()
        self.terminal(original)
        self.git('commit', '--allow-empty', '-qm', 'new head')
        result = self.cli('retry', original['id'], '--json')
        retry = core.find_job(self.root, result['id'])
        self.assertEqual(retry['freshness'], 'stale')
        self.assertEqual(retry['context_snapshot'], original['context_snapshot'])
        self.assertEqual(retry['context_text'], original['context_text'])
        self.assertTrue(any('base_sha changed' in w for w in result['warnings']))
        self.assertNotIn('resume_session_id', retry)
        self.assertEqual(core.find_job(self.root, original['id']), original)

    def test_dirty_snapshot_is_fresh_until_existing_dirty_content_changes(self):
        (self.cwd / 'notes.md').write_text('dirty A\n')
        original = self.submit()
        self.assertEqual(core.check_context_freshness(original), 'fresh')
        (self.cwd / 'notes.md').write_text('dirty B\n')
        self.assertEqual(core.check_context_freshness(original), 'stale')
        self.assertTrue(any('drifted' in w for w in original['warnings']))

    def test_untracked_edits_and_clean_to_dirty_and_staged_changes(self):
        for kind in ('untracked', 'tracked', 'staged', 'deleted'):
            with self.subTest(kind=kind):
                path = self.cwd / ('new.txt' if kind == 'untracked' else 'notes.md')
                path.write_text('before\n')
                snapshot = core.capture_context_snapshot(self.cwd, ['notes.md'])
                if kind == 'deleted':
                    path.unlink()
                else:
                    path.write_text('after!\n')
                if kind == 'staged':
                    self.git('add', 'notes.md')
                job = {'cwd': str(self.cwd), 'context_snapshot': snapshot}
                self.assertEqual(core.check_context_freshness(job), 'stale')

    def test_transient_retry_and_explicit_resume_check_original_snapshot(self):
        original = self.submit()
        self.terminal(original)
        (self.cwd / 'other.txt').write_text('drift')
        retry = core.find_job(self.root, core.retry_transient(self.root, original))
        self.assertEqual(retry['freshness'], 'stale')
        self.assertEqual(retry['context_snapshot'], original['context_snapshot'])
        self.assertTrue(retry['warnings'])
        self.cli('submit', '--id', 'resumed', '--project', 'fixture', '--area', 'backend',
                 '--tier', 'M', '--cwd', str(self.cwd), '--files', 'notes.md', '--prompt-text', 'continue',
                 '--resume-of', original['id'], '--no-tick', '--json')
        resumed = core.find_job(self.root, 'resumed')
        self.assertEqual(resumed['freshness'], 'stale')
        self.assertEqual(resumed['context_snapshot'], original['context_snapshot'])

    def test_status_rechecks_without_persisting_and_handoff_warns(self):
        original = self.submit()
        (self.cwd / 'notes.md').write_text('changed')
        result = self.cli('status', original['id'], '--no-reclaim', '--json')['job']
        self.assertEqual(result['freshness'], 'stale')
        self.assertEqual(result['handoff']['freshness'], 'stale')
        self.assertTrue(any('snapshot stale' in risk for risk in result['handoff']['risks']))
        self.assertEqual(core.find_job(self.root, original['id']), original)

    def test_legacy_non_git_and_query_failure_are_unknown(self):
        for snapshot in (None, {}, {'paths': None}, {'captured_at': 'old', 'paths': [None]}):
            job = {'id': 'old', 'cwd': str(self.cwd), 'base_sha': self.git('rev-parse', 'HEAD'),
                   'context_text': 'legacy', 'context_snapshot': snapshot}
            self.assertEqual(core.job_summary(job)['freshness'], 'unknown')
            self.assertEqual(core.job_summary(job)['handoff']['freshness'], 'unknown')
        snapshot = core.capture_context_snapshot(self.base, ['repo/notes.md'])
        self.assertIsNone(snapshot['base_sha'])
        self.assertEqual(core.check_context_freshness({'cwd': str(self.base), 'context_snapshot': snapshot}), 'unknown')
        original = self.submit()
        with patch.object(core, 'git_output', return_value=None):
            self.assertEqual(core.check_context_freshness(original), 'unknown')
        self.terminal(dict(original, context_snapshot=None))
        retry = self.cli('retry', original['id'], '--json')['job']
        self.assertEqual(retry['freshness'], 'unknown')

    def test_external_source_paths_detect_drift(self):
        source = self.base / 'external.md'
        source.write_text('before')
        snapshot = core.capture_context_snapshot(self.cwd, [str(source) + ':1-1'])
        source.write_text('after')
        self.assertEqual(core.check_context_freshness({'cwd': str(self.cwd), 'context_snapshot': snapshot}), 'stale')
