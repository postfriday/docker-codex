"""Exercise the complete coordinator against durable fake external services.

Faults happen after the external mutation and before its receipt: retry restores
only the persisted journal, while registry/assets/analyses retain their effects.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_release import a, coordinator, active, CURRENT, ENV, MemoryStore


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, dict(ENV, RUNNER_TEMP=self.directory.name))
        self.environment.start()
        self.c = coordinator()
        self.r = active(self.c)
        self.r['attempts']['100:1'] = {'status': 'running'}
        self.c.store.save()
        self.registry, self.assets, self.analyses = {}, {}, {}
        self.attested = False
        self.counts = {'build': 0, 'scan': 0, 'cli': 0, 'upload': 0, 'sarif': 0, 'tag': 0}
        self.fault = None
        self.configure()
        self.command_patch = patch('automation.run', side_effect=self.command)
        self.api_patch = patch('automation.api', side_effect=self.api)
        self.cleanup_patch = patch('automation.subprocess.run')
        self.output_patch = patch('automation.output')
        for fixture in [self.command_patch, self.api_patch, self.cleanup_patch, self.output_patch]:
            fixture.start()

    def tearDown(self):
        for fixture in [self.command_patch, self.api_patch, self.cleanup_patch, self.output_patch]:
            fixture.stop()
        self.environment.stop()
        self.directory.cleanup()

    def configure(self):
        self.c.verify_identity = Mock()
        self.c.inspect_candidate = Mock(return_value={'amd64': 'sha256:amd64', 'arm64': 'sha256:arm64'})
        self.c.digest = lambda ref, missing=False: self.registry.get(ref.rsplit(':', 1)[-1])
        self.c.ensure_release = Mock()
        self.c.asset = lambda record, name: {'url': 'https://example.invalid/' + name} if name in self.assets else None
        self.c.verify_attestation = lambda record: self.assertTrue(self.attested)

    def crash(self, point):
        if self.fault == point:
            self.fault = None
            raise RuntimeError('Lost response after ' + point)

    def restore(self):
        state = copy.deepcopy(self.c.store.persisted)
        self.c.store = MemoryStore(state)
        self.c.state, self.c.records = state, state['releases']
        self.r = self.c.records['v1.0.1']
        self.configure()

    def command(self, *args, **kwargs):
        if args[:2] == ('git', 'checkout'):
            return ''
        if args[:3] == ('docker', 'buildx', 'build'):
            self.counts['build'] += 1
            self.registry['candidate-v1.0.1'] = 'sha256:candidate'
            self.crash('build')
        elif args[:2] == ('docker', 'run'):
            if 'hadolint' in args:
                return ''
            self.counts['cli'] += 1
            cli = args[args.index('--entrypoint') + 1].split('/')[-1]
            version = CURRENT[cli]
            return {'codex': f'codex-cli {version}', 'opencode': version,
                    'claude': f'{version} (Claude Code)'}[cli]
        elif args[:2] == ('trivy', 'image'):
            self.counts['scan'] += 1
            self.crash('scan')
            target = Path(args[args.index('--output') + 1])
            target.write_text(json.dumps({'version': '2.1.0', 'runs': [
                {'tool': {'driver': {'name': 'Trivy'}}, 'results': [
                    {'level': 'error', 'message': {'text': 'CVE fixture'}}]}]}))
        elif args[:3] == ('gh', 'release', 'upload'):
            self.counts['upload'] += 1
            path = Path(args[4])
            self.assets[path.name] = path.read_bytes()
            self.crash('upload')
        elif args[:3] == ('gh', 'release', 'download'):
            name = args[args.index('--pattern') + 1]
            Path(args[args.index('--output') + 1]).write_bytes(self.assets[name])
        elif args[:3] == ('docker', 'buildx', 'imagetools'):
            self.counts['tag'] += 1
            tag = args[args.index('--tag') + 1].rsplit(':', 1)[-1]
            self.registry[tag] = 'sha256:candidate'
            self.crash('tag')
        elif args[:3] == ('gh', 'release', 'edit'):
            self.crash('release-edit')
        else:
            raise AssertionError(args)
        return ''

    def api(self, path, method='GET', data=None):
        if '/attestations/' in path:
            return {'attestations': [{'id': 1}] if self.attested else []}
        if '/code-scanning/analyses?' in path:
            return list(self.analyses.values())
        if path.endswith('/code-scanning/sarifs') and method == 'POST':
            self.counts['sarif'] += 1
            import base64
            import gzip
            report = json.loads(gzip.decompress(base64.b64decode(data['sarif'])))
            upload_id = str(self.counts['sarif'])
            self.analyses[upload_id] = {
                'commit_sha': data['commit_sha'], 'sarif_id': upload_id,
                'category': report['runs'][0]['automationDetails']['id']}
            self.crash('sarif')
            return {'id': upload_id}
        if '/code-scanning/sarifs/' in path:
            return {'processing_status': 'complete'}
        raise AssertionError((path, method, data))

    def finish(self):
        self.c.prepare('v1.0.1')
        self.attested = True  # Represents the separate Actions attestation step.
        self.c.finish('v1.0.1')
        self.assertEqual(self.r['status'], 'completed')
        self.assertEqual(self.r['candidate_digest'], 'sha256:candidate')
        self.assertEqual(len(self.r['published_tags']), 6)
        self.assertEqual(self.counts['build'], 1)
        self.assertEqual(self.counts['cli'], 6)
        self.assertEqual(self.counts['upload'], 2)
        self.assertEqual(self.counts['sarif'], 2)
        self.assertEqual(self.counts['tag'], 6)
        for name, receipt in self.r['reports'].items():
            self.assertEqual(receipt['sha256'], hashlib.sha256(self.assets[name]).hexdigest())

    def test_success_with_vulnerability_findings(self):
        self.finish()
        self.assertEqual(self.counts['scan'], 2)

    def test_saved_candidate_recovers_after_lost_build_response(self):
        self.fault = 'build'
        with self.assertRaises(RuntimeError):
            self.c.prepare('v1.0.1')
        self.restore()
        self.assertIsNone(self.r['candidate_digest'])
        self.finish()

    def test_uploaded_asset_recovers_without_rescan_or_reupload(self):
        self.fault = 'upload'
        with self.assertRaises(RuntimeError):
            self.c.prepare('v1.0.1')
        self.restore()
        self.finish()
        self.assertEqual(self.counts['scan'], 2)

    def test_scan_technical_failure_blocks_all_final_tags(self):
        self.fault = 'scan'
        with self.assertRaises(RuntimeError):
            self.c.prepare('v1.0.1')
        self.assertEqual(self.counts['tag'], 0)
        self.restore()
        self.finish()
        self.assertEqual(self.counts['scan'], 3)

    def test_sarif_response_loss_recovers_existing_analysis(self):
        self.c.prepare('v1.0.1')
        self.attested = True
        self.fault = 'sarif'
        with self.assertRaises(RuntimeError):
            self.c.finish('v1.0.1')
        self.restore()
        self.finish()

    def test_partial_tag_publication_recovers_without_duplicate_assignment(self):
        self.c.prepare('v1.0.1')
        self.attested = True
        self.fault = 'tag'
        with self.assertRaises(RuntimeError):
            self.c.finish('v1.0.1')
        self.restore()
        self.finish()

    def test_all_tags_before_completion_does_not_repeat_registry_writes(self):
        self.c.prepare('v1.0.1')
        self.attested = True
        self.fault = 'release-edit'
        with self.assertRaises(RuntimeError):
            self.c.finish('v1.0.1')
        self.restore()
        self.finish()
