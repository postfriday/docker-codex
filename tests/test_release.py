import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'release'))
import automation as a
import model as m

DOCKERFILE = 'FROM node:24-slim\nARG CODEX_VERSION=0.1.0\nARG OPENCODE_VERSION=1.8.0\nARG CLAUDE_CODE_VERSION=2.0.0\n'
CURRENT = m.versions(DOCKERFILE)
ENV = {'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_ACTOR': 'tester'}


def npm(latest, *items):
    return {'dist-tags': {'latest': latest}, 'versions': {v: {'version': v} for v in (latest, *items)}}


def responses():
    return {cli: npm(version) for cli, version in CURRENT.items()}


class MemoryStore:
    def __init__(self, state):
        self.state = state
        self.persisted = copy.deepcopy(state)
        self.saves = 0

    def save(self):
        m.validate_state(self.state)
        self.persisted = copy.deepcopy(self.state)
        self.saves += 1


def coordinator():
    c = object.__new__(a.Coordinator)
    c.repo, c.image, c.branch = 'owner/repo', 'ghcr.io/owner/repo', 'main'
    baseline = c.record('v1.0.0', 'a' * 40, DOCKERFILE, 'baseline', CURRENT, 'a' * 40)
    baseline.update(status='imported', candidate_digest='sha256:old')
    c.state = {'schema_version': 1, 'baseline': 'v1.0.0', 'releases': {'v1.0.0': baseline}}
    c.records = c.state['releases']
    c.store = MemoryStore(c.state)
    return c


def active(c, version='1.0.1'):
    record = c.record('v' + version, 'b' * 40, DOCKERFILE, 'npm', CURRENT, 'a' * 40)
    c.records[record['release_id']] = record
    return record


class VersionTests(unittest.TestCase):
    def test_numeric_order_and_zero_major(self):
        data = responses()
        data['opencode'] = npm('1.9.0', '1.10.0', '1.11.0-rc.1', '1.99.0+build')
        data['codex'] = npm('0.2.0')
        selected, majors = m.select(CURRENT, data)
        self.assertEqual(selected['opencode'], '1.10.0')
        self.assertEqual(selected['codex'], '0.2.0')
        self.assertEqual(majors, [])

    def test_new_major_keeps_current_line(self):
        data = responses()
        data['opencode'] = npm('2.0.0', '1.9.1')
        selected, majors = m.select(CURRENT, data)
        self.assertEqual(selected['opencode'], '1.9.1')
        self.assertEqual(majors, [('opencode-ai', 2, '2.0.0')])

    def test_older_latest_never_downgrades(self):
        data = responses()
        data['opencode'] = npm('1.0.0')
        self.assertEqual(m.select(CURRENT, data)[0], CURRENT)

    def test_no_updates(self):
        self.assertEqual(m.select(CURRENT, responses()), (CURRENT, []))

    def test_invalid_latest_or_metadata_aborts(self):
        for malformed in [npm('2.0.0-rc.1'), {'versions': {}},
                          {'dist-tags': {'latest': '1.0.0'}, 'versions': []},
                          {'dist-tags': {'latest': '1.0.0'}, 'versions': {'1.0.0': {}}}]:
            with self.subTest(malformed=malformed):
                data = responses()
                data['claude'] = malformed
                with self.assertRaises((ValueError, KeyError)):
                    m.select(CURRENT, data)
                self.assertEqual(CURRENT, m.versions(DOCKERFILE))

    def test_arg_validation(self):
        for dockerfile in [DOCKERFILE + 'ARG CODEX_VERSION=0.1.0\n',
                           DOCKERFILE.replace('0.1.0', '0.1.0-beta'),
                           DOCKERFILE.replace('ARG CODEX_VERSION=0.1.0\n', '')]:
            with self.assertRaises(ValueError):
                m.versions(dockerfile)

    def test_replace_only_selected_args(self):
        selected = dict(CURRENT, codex='0.2.0')
        self.assertEqual(m.replace_versions(DOCKERFILE, selected), DOCKERFILE.replace('0.1.0', '0.2.0'))

    def test_composition_includes_recipe(self):
        self.assertNotEqual(m.composition(DOCKERFILE), m.composition(DOCKERFILE + '# fix\n'))

    def test_exact_cli_output(self):
        for cli, output, expected in [('codex', 'codex-cli 0.1.0\n', '0.1.0'),
                                      ('opencode', '1.8.0', '1.8.0'),
                                      ('claude', '2.0.0 (Claude Code)', '2.0.0')]:
            self.assertEqual(m.cli_version(cli, output), expected)
            for invalid in ['warning ' + output, output + 'unexpected', output.replace(expected, expected + '-rc.1')]:
                with self.assertRaises(ValueError):
                    m.cli_version(cli, invalid)

    def test_invalid_ids(self):
        for value in ['main', 'v1.2.3-rc.1', '1.2.3', 'v01.2.3', 'v1.2.3\nother']:
            with self.assertRaises(ValueError):
                m.release_id(value)


class StateTests(unittest.TestCase):
    def test_unknown_schema_or_multiple_active(self):
        c = coordinator()
        active(c)
        active(c, '1.0.2')
        with self.assertRaises(ValueError):
            m.validate_state(c.state)
        c.state['schema_version'] = 2
        with self.assertRaises(ValueError):
            m.validate_state(c.state)

    @patch.dict(os.environ, ENV)
    def test_cancel_is_idempotent_and_resume_rejected(self):
        c = coordinator()
        r = active(c)
        c.verify_identity = Mock()
        c.control('cancel', r['release_id'], 'bad image')
        first = copy.deepcopy(r)
        c.control('cancel', r['release_id'], 'different reason')
        self.assertEqual(r, first)
        self.assertEqual(r['cancellation']['actor'], 'tester')
        with self.assertRaises(ValueError):
            c.dispatch(r)

    def test_cancel_completed_or_missing_reason_rejected(self):
        c = coordinator()
        r = active(c)
        c.verify_identity = Mock()
        with self.assertRaises(ValueError):
            c.control('cancel', r['release_id'], '')
        r['status'] = 'completed'
        with self.assertRaises(ValueError):
            c.control('cancel', r['release_id'], 'reason')

    @patch('automation.api')
    def test_interrupted_attempt_counted_once(self, api):
        c = coordinator()
        r = active(c)
        r['attempts']['1:1'] = {'status': 'running', 'run_id': '1', 'run_attempt': '1'}
        api.return_value = {'status': 'completed'}
        c.recover_attempts(r)
        c.recover_attempts(r)
        self.assertEqual(r['failure_count'], 1)

    @patch('automation.api')
    def test_running_attempt_not_counted_as_failure(self, api):
        c = coordinator()
        r = active(c)
        r['attempts']['1:1'] = {'status': 'running', 'run_id': '1', 'run_attempt': '1'}
        api.return_value = {'status': 'in_progress'}
        with self.assertRaises(ValueError):
            c.recover_attempts(r)
        self.assertEqual(r['failure_count'], 0)

    @patch('automation.api')
    def test_issue_recovered_from_closed_issues(self, api):
        c = coordinator()
        api.return_value = [{'number': 42, 'state': 'closed', 'body': '<!-- docker-codex:major:opencode-ai:2 -->'}]
        self.assertEqual(c.issue('major:opencode-ai:2', 'title', 'body'), 42)
        self.assertEqual(api.call_count, 1)

    def test_failure_issue_retry(self):
        c = coordinator()
        r = active(c)
        r['failure_count'] = 3
        c.issue = Mock(side_effect=[RuntimeError('network'), 42])
        c.failure_notice(r)
        c.failure_notice(r)
        c.failure_notice(r)
        self.assertEqual(c.issue.call_count, 2)
        self.assertEqual(r['failure_issue'], 42)

    @patch('automation.api')
    def test_dispatch_uses_release_ref(self, api):
        c = coordinator()
        r = active(c)
        c.verify_identity = Mock()
        c.dispatch(r)
        self.assertEqual(api.call_args.args[2]['ref'], r['git_tag'])

    @patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main'})
    def test_publishing_from_branch_rejected_before_mutation(self):
        c = coordinator()
        c.register_manual = Mock()
        with self.assertRaisesRegex(ValueError, 'exact release tag'):
            c.begin('v1.0.1')
        c.register_manual.assert_not_called()

    def test_active_release_takes_priority_over_npm(self):
        c = coordinator()
        r = active(c)
        c.dispatch, c.recover_attempts = Mock(), Mock()
        with patch('automation.urllib.request.urlopen', side_effect=AssertionError('npm used')):
            c.control('check')
        c.dispatch.assert_called_once_with(r)


class TagTests(unittest.TestCase):
    def test_immutable_tag_conflict_and_unknown_alias(self):
        c = coordinator()
        r = active(c)
        r['candidate_digest'] = 'sha256:new'
        for observed in [{'1.0.1': 'sha256:foreign'}, {'latest': 'sha256:foreign'}]:
            with self.assertRaises(ValueError):
                m.tag_plan(r, c.records, observed)

    def test_old_release_never_regresses_aliases(self):
        c = coordinator()
        old = c.records['v1.0.0']
        r = active(c)
        r.update(status='completed', candidate_digest='sha256:new',
                 published_tags={tag: 'sha256:new' for tag in ['latest', '1', '1.0']})
        plan = dict(m.tag_plan(old, c.records, {tag: 'sha256:new' for tag in ['latest', '1', '1.0']}))
        self.assertTrue(all(plan[tag] == 'superseded' for tag in ['latest', '1', '1.0']))

    def test_crash_after_registry_assignment_before_receipt(self):
        c = coordinator()
        r = active(c)
        r['candidate_digest'] = 'sha256:new'
        c.inspect_candidate = Mock()
        registry = {r['candidate_tag']: 'sha256:new'}
        c.digest = lambda ref, missing=False: registry.get(ref.split(':')[-1])
        calls = []

        def assign(*args, **kwargs):
            tag = args[5].split(':')[-1]
            registry[tag] = 'sha256:new'
            calls.append(tag)
            if len(calls) == 1:
                raise RuntimeError('lost push response')

        with patch('automation.run', side_effect=assign):
            with self.assertRaises(RuntimeError):
                c.promote(r)
        self.assertNotIn('1.0.1', r['published_tags'])
        with patch('automation.run', side_effect=assign):
            c.promote(r)
        self.assertEqual(calls.count('1.0.1'), 1)
        self.assertEqual(len(r['published_tags']), 6)

    def test_cancelled_publication_does_not_write_registry(self):
        c = coordinator()
        r = active(c)
        r['status'] = 'cancelled'
        with patch('automation.run') as run:
            with self.assertRaises(ValueError):
                c.promote(r)
            run.assert_not_called()

    def test_missing_candidate_never_rebuilds(self):
        c = coordinator()
        r = active(c)
        r['candidate_digest'] = 'sha256:saved'
        c.verify_identity = Mock()
        c.digest = Mock(return_value=None)
        with patch('automation.run') as run, patch('automation.output'):
            with self.assertRaisesRegex(ValueError, 'rebuilding is forbidden'):
                c.prepare(r['release_id'])
            self.assertFalse(any(call.args[:3] == ('docker', 'buildx', 'build') for call in run.call_args_list))

    def test_digest_persisted_before_evidence_failure(self):
        c = coordinator()
        r = active(c)
        c.verify_identity = Mock()
        c.digest = Mock(return_value='sha256:recovered')
        c.inspect_candidate = Mock(side_effect=[{'amd64': 'a', 'arm64': 'b'}, ValueError('missing SBOM')])
        with patch('automation.run') as run, patch('automation.output'):
            with self.assertRaisesRegex(ValueError, 'missing SBOM'):
                c.prepare(r['release_id'])
        saved = c.store.persisted['releases'][r['release_id']]
        self.assertEqual(saved['candidate_digest'], 'sha256:recovered')
        self.assertEqual(saved['status'], 'candidate_saved')
        self.assertEqual(run.call_count, 1)

    def test_complete_retry_is_readonly_and_never_builds(self):
        c = coordinator()
        r = active(c)
        r.update(status='completed', candidate_digest='sha256:saved', platform_digests={'amd64': 'a', 'arm64': 'b'})
        for name in ['verify_identity', 'verify_reports', 'verify_attestation', 'promote', 'succeed']:
            setattr(c, name, Mock())
        c.digest = Mock(return_value='sha256:saved')
        c.inspect_candidate = Mock(return_value=r['platform_digests'])
        with patch('automation.run') as run, patch('automation.output'):
            c.prepare(r['release_id'])
            self.assertEqual(run.call_count, 1)  # checkout only
        c.promote.assert_called_once_with(r, readonly=True)

    def test_registry_outage_is_not_absence(self):
        c = coordinator()
        with patch('automation.run', side_effect=RuntimeError('UNAUTHORIZED')):
            with self.assertRaises(RuntimeError):
                c.digest('image:tag', missing=True)
        with patch('automation.run', side_effect=RuntimeError('MANIFEST_UNKNOWN')):
            self.assertIsNone(c.digest('image:tag', missing=True))

    def test_platform_descriptor_must_match_image_config(self):
        c = coordinator()
        r = active(c)
        manifest = {'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'},
                                   'digest': 'sha256:a'}]}
        config = {'os': 'linux', 'architecture': 'arm64', 'config': {'Labels': {
            'org.opencontainers.image.revision': r['release_sha'],
            'org.opencontainers.image.version': r['image_version']}}}
        with patch('automation.run', side_effect=[json.dumps(manifest), json.dumps(config)]):
            with self.assertRaisesRegex(ValueError, 'platform disagree'):
                c.inspect_candidate(r, 'sha256:index')

    def test_partial_platform_candidate_rejected(self):
        c = coordinator()
        r = active(c)
        with patch('automation.run', return_value=json.dumps({'manifests': []})):
            with self.assertRaisesRegex(ValueError, 'complete AMD64/ARM64'):
                c.inspect_candidate(r, 'sha256:x')


class AttestationTests(unittest.TestCase):
    @patch('automation.api')
    def test_lost_receipt_reuses_verified_attestation(self, api):
        c = coordinator()
        r = active(c)
        r['candidate_digest'] = 'sha256:candidate'
        c.verify_attestation = Mock()
        api.return_value = {'attestations': [{'id': 1}]}
        self.assertTrue(c.restore_attestation(r))
        self.assertEqual(r['attestation']['digest'], 'sha256:candidate')
        c.verify_attestation.assert_called_once_with(r)

    @patch('automation.api')
    def test_partial_attestation_creation_retries_registry_upload(self, api):
        c = coordinator()
        r = active(c)
        c.verify_attestation = Mock(side_effect=RuntimeError('Registry bundle missing'))
        api.return_value = {'attestations': [{'id': 1}]}
        self.assertFalse(c.restore_attestation(r))
        self.assertIsNone(r['attestation'])

    @patch('automation.run')
    def test_both_github_and_registry_are_verified(self, run):
        c = coordinator()
        r = active(c)
        c.verify_attestation(r)
        self.assertEqual(run.call_count, 2)
        self.assertIn('--bundle-from-oci', run.call_args_list[1].args)
        args = run.call_args_list[0].args
        self.assertEqual(args[args.index('--source-digest') + 1], r['release_sha'])


class ScanTests(unittest.TestCase):
    @patch('automation.api')
    def test_successful_upload_is_recovered_after_lost_receipt(self, api):
        c = coordinator()
        r = active(c)
        r['candidate_digest'] = 'sha256:candidate'
        r['platform_digests'] = {'amd64': 'sha256:platform'}
        r['reports'] = {'trivy-amd64.sarif': {'sha256': 'checksum'}}
        api.side_effect = [[{'commit_sha': r['release_sha'],
                             'category': 'trivy-amd64/sha256:candidate/', 'sarif_id': 'upload-1'}],
                           {'processing_status': 'complete'}]
        c.upload_sarif(r)
        receipt = r['reports']['trivy-amd64.sarif']
        self.assertEqual(receipt['sarif_id'], 'upload-1')
        self.assertEqual(receipt['sarif_status'], 'complete')
        self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))  # GET only

    def test_findings_allowed_but_bad_reports_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.sarif'
            document = {'version': '2.1.0', 'properties': {'release_digest': 'sha256:a'},
                        'runs': [{'tool': {'driver': {'name': 'Trivy'}},
                                  'results': [{'level': 'error', 'message': {'text': 'CVE'}}]}]}
            path.write_text(json.dumps(document))
            a.Coordinator.validate_report(path, 'sha256:a')
            with self.assertRaises(ValueError):
                a.Coordinator.validate_report(path, 'sha256:b')
            path.write_text('{}')
            with self.assertRaises(ValueError):
                a.Coordinator.validate_report(path, 'sha256:a')


class AtomicGitTests(unittest.TestCase):
    def test_concurrent_branch_change_rejects_tag_and_state_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local, other = (root / name for name in ['remote.git', 'local', 'other'])
            a.run('git', 'init', '--bare', str(remote))
            a.run('git', 'clone', str(remote), str(local))
            for work in [local]:
                a.run('git', 'config', 'user.name', 'test', cwd=work)
                a.run('git', 'config', 'user.email', 'test@example.com', cwd=work)
            (local / 'VERSION').write_text('1.0.0\n')
            a.run('git', 'add', '.', cwd=local)
            a.run('git', 'commit', '-m', 'initial', cwd=local)
            a.run('git', 'branch', '-M', 'main', cwd=local)
            a.run('git', 'push', 'origin', 'main', cwd=local)
            a.run('git', 'clone', '-b', 'main', str(remote), str(other))
            a.run('git', 'config', 'user.name', 'test', cwd=other)
            a.run('git', 'config', 'user.email', 'test@example.com', cwd=other)
            (other / 'README').write_text('concurrent change')
            a.run('git', 'add', '.', cwd=other)
            a.run('git', 'commit', '-m', 'concurrent', cwd=other)
            a.run('git', 'push', 'origin', 'main', cwd=other)
            (local / 'VERSION').write_text('1.0.1\n')
            a.run('git', 'commit', '-am', 'release', cwd=local)
            a.run('git', 'tag', '-a', 'v1.0.1', '-m', 'release', cwd=local)
            with self.assertRaises(RuntimeError):
                a.run('git', 'push', '--atomic', 'origin', 'HEAD:refs/heads/main',
                      'refs/tags/v1.0.1:refs/tags/v1.0.1', 'HEAD:refs/heads/release-state', cwd=local)
            self.assertEqual(a.run('git', 'ls-remote', str(remote), 'refs/tags/v1.0.1', 'refs/heads/release-state'), '')

class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.previous = Path.cwd()
        remote, local = self.root / 'remote.git', self.root / 'local'
        a.run('git', 'init', '--bare', str(remote))
        a.run('git', 'clone', str(remote), str(local))
        os.chdir(local)
        a.run('git', 'config', 'user.name', 'test')
        a.run('git', 'config', 'user.email', 'test@example.com')
        Path('Dockerfile').write_text(DOCKERFILE)
        Path('VERSION').write_text('1.0.0\n')
        a.run('git', 'add', '.')
        a.run('git', 'commit', '-m', 'baseline')
        a.run('git', 'branch', '-M', 'main')
        a.run('git', 'tag', '-a', 'v1.0.0', '-m', 'baseline')
        a.run('git', 'push', 'origin', 'main', '--tags')
        self.c = coordinator()
        baseline = self.c.records['v1.0.0']
        baseline['release_sha'] = a.run('git', 'rev-parse', 'HEAD')
        self.c.store = a.Store(initialize=True)
        self.c.store.state = self.c.state
        self.c.store.save()
        self.c.dispatch = Mock()
        self.c.issue = Mock()

    def tearDown(self):
        os.chdir(self.previous)
        self.directory.cleanup()

    def fetch_npm(self, data):
        return patch('automation.urllib.request.urlopen', side_effect=[
            io.BytesIO(json.dumps(data[cli]).encode()) for cli in m.TOOLS])

    def test_no_update_does_not_commit(self):
        before = a.run('git', 'rev-parse', 'HEAD')
        with self.fetch_npm(responses()):
            self.c.control('check')
        self.assertEqual(a.run('git', 'rev-parse', 'HEAD'), before)
        self.c.dispatch.assert_not_called()

    def test_three_updates_one_atomic_reservation(self):
        data = {cli: npm(version) for cli, version in
                dict(codex='0.2.0', opencode='1.9.0', claude='2.1.0').items()}
        with self.fetch_npm(data):
            self.c.control('check')
        self.assertEqual(Path('VERSION').read_text(), '1.0.1\n')
        self.assertEqual(len(self.c.records), 2)
        self.assertEqual(a.run('git', 'cat-file', '-t', 'v1.0.1'), 'tag')
        self.c.dispatch.assert_called_once()
        # A fresh store after a lost dispatch response contains the same reservation.
        restored = a.Store()
        self.assertEqual(m.validate_state(restored.state)['release_id'], 'v1.0.1')
        self.c.control('check')
        self.assertEqual(len(self.c.records), 2)

    def test_bad_npm_response_leaves_all_files_untouched(self):
        data = responses()
        data['codex'] = npm('0.2.0')
        data['claude'] = {}
        with self.fetch_npm(data), self.assertRaises(KeyError):
            self.c.control('check')
        self.assertEqual(Path('Dockerfile').read_text(), DOCKERFILE)
        self.assertEqual(a.run('git', 'status', '--porcelain'), '')
        self.c.dispatch.assert_not_called()

    def test_manual_composition_precedes_npm(self):
        changed = DOCKERFILE.replace('1.8.0', '2.0.0')
        Path('Dockerfile').write_text(changed)
        a.run('git', 'commit', '-am', 'accept major')
        a.run('git', 'push', 'origin', 'main')
        with patch('automation.urllib.request.urlopen', side_effect=AssertionError('npm queried')):
            self.c.control('check')
        self.assertEqual(Path('Dockerfile').read_text(), changed)
        self.assertEqual(self.c.records['v1.0.1']['origin'], 'manual-composition')

    @patch.dict(os.environ, ENV)
    def test_cancelled_composition_only_restarts_after_recipe_fix(self):
        changed = DOCKERFILE.replace('1.8.0', '1.9.0')
        Path('Dockerfile').write_text(changed)
        a.run('git', 'commit', '-am', 'update cli')
        a.run('git', 'push', 'origin', 'main')
        self.c.control('check')
        self.c.control('cancel', 'v1.0.1', 'broken recipe')
        with self.fetch_npm(responses()):
            self.c.control('check')
        self.assertEqual(len(self.c.records), 2)
        Path('Dockerfile').write_text(changed + '# recipe fix\n')
        a.run('git', 'commit', '-am', 'fix recipe')
        a.run('git', 'push', 'origin', 'main')
        self.c.control('check')
        self.assertEqual(Path('VERSION').read_text(), '1.0.2\n')


class CandidateChecks(unittest.TestCase):
    @patch.dict(os.environ, ENV)
    def test_each_of_six_checks_blocks_on_wrong_version(self):
        for failing_arch in ['amd64', 'arm64']:
            for failing_cli in m.TOOLS:
                with self.subTest(arch=failing_arch, cli=failing_cli), tempfile.TemporaryDirectory() as directory:
                    c = coordinator()
                    r = active(c)
                    r.update(candidate_digest='sha256:saved', platform_digests={'amd64': 'a', 'arm64': 'b'},
                             reports={f'trivy-{arch}.sarif': {'sha256': 'x'} for arch in ['amd64', 'arm64']})
                    c.verify_identity, c.download_report = Mock(), Mock()
                    c.digest = Mock(return_value='sha256:saved')
                    c.inspect_candidate = Mock(return_value=r['platform_digests'])

                    def command(*args, **kwargs):
                        if args[0] == 'git':
                            return ''
                        self.assertEqual(args[:3], ('docker', 'run', '--rm'))
                        arch = args[args.index('--platform') + 1].split('/')[1]
                        cli = args[args.index('--entrypoint') + 1].split('/')[-1]
                        version = '9.9.9' if (arch, cli) == (failing_arch, failing_cli) else CURRENT[cli]
                        return {'codex': f'codex-cli {version}', 'opencode': version,
                                'claude': f'{version} (Claude Code)'}[cli]

                    with patch('automation.run', side_effect=command), patch('automation.output'), \
                         patch('automation.subprocess.run'), patch.dict(os.environ, {'RUNNER_TEMP': directory}):
                        with self.assertRaisesRegex(ValueError, 'expected .* got'):
                            c.prepare(r['release_id'])
                    self.assertEqual(r['published_tags'], {})
                    self.assertNotIn(f'{failing_arch}:{failing_cli}', r['checks'])


if __name__ == "__main__":
    unittest.main()
