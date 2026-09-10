#!/usr/bin/env python3
"""Release coordinator. All mutating invocations require the shared Actions lock."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from model import (TOOLS, TERMINAL, cli_version, composition, release_id, replace_versions,
                   select, semver, tag_plan, validate_state, versions, STABLE)

BUILD_PROVENANCE_TYPES = {
    'https://slsa.dev/provenance/v0.2',
    'https://slsa.dev/provenance/v1',
}


def run(*args, cwd=None, input=None, timeout=300, live=False):
    result = subprocess.run(args, cwd=cwd, input=input, text=True, capture_output=not live,
                            timeout=timeout, check=False)
    if result.returncode:
        detail = (result.stderr or 'See command output in the Actions log').strip()[-12000:]
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {detail}')
    return (result.stdout or '').strip()


def now():
    return datetime.now(timezone.utc).isoformat()


def api(path, method='GET', data=None):
    args = ['gh', 'api', path, '--method', method]
    if data is not None:
        args += ['--input', '-']
    output = run(*args, input=json.dumps(data) if data is not None else None)
    return json.loads(output) if output else None


def output(key, value):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'{key}={value}\n')


def summary(message):
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(message + '\n')


class Store:
    """Append-only Git history, ordinary fast-forward pushes, atomic reservation."""
    def __init__(self, initialize=False):
        self.root = Path(tempfile.mkdtemp(prefix='release-state-'))
        run('git', 'init', '-q', str(self.root))
        run('git', 'remote', 'add', 'origin', run('git', 'remote', 'get-url', 'origin'), cwd=self.root)
        # checkout's token is scoped to its worktree; copy the header without logging it.
        config = subprocess.run(['git', 'config', '--get-regexp', r'http\..*\.extraheader'],
                                capture_output=True, text=True, check=False)
        for line in config.stdout.splitlines():
            key, value = line.split(' ', 1)
            run('git', 'config', key, value, cwd=self.root)
        run('git', 'config', 'user.name', 'github-actions[bot]', cwd=self.root)
        run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=self.root)
        exists = run('git', 'ls-remote', '--heads', 'origin', 'refs/heads/release-state')
        if exists:
            run('git', 'fetch', 'origin', 'refs/heads/release-state', cwd=self.root)
            run('git', 'checkout', '-q', '-b', 'release-state', 'FETCH_HEAD', cwd=self.root)
            self.state = json.loads((self.root / 'state.json').read_text())
            validate_state(self.state)
        elif initialize:
            run('git', 'checkout', '--orphan', 'release-state', cwd=self.root)
            self.state = {'schema_version': 1, 'baseline': None, 'releases': {}}
        else:
            raise ValueError('release-state is missing. Do not recreate it: import explicitly or restore backup.')

    def commit(self):
        validate_state(self.state)
        (self.root / 'state.json').write_text(json.dumps(self.state, indent=2) + '\n')
        run('git', 'add', 'state.json', cwd=self.root)
        if run('git', 'status', '--porcelain', cwd=self.root):
            run('git', 'commit', '-qm', 'chore: record release progress', cwd=self.root)
        return run('git', 'rev-parse', 'HEAD', cwd=self.root)

    def save(self):
        self.commit()
        run('git', 'push', 'origin', 'HEAD:refs/heads/release-state', cwd=self.root)


class Coordinator:
    def __init__(self, initialize=False):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.image = f'ghcr.io/{self.repo.lower()}'
        self.store = Store(initialize)
        self.state = self.store.state
        self.records = self.state['releases']
        self.branch = os.environ['DEFAULT_BRANCH']

    def issue(self, key, title, body):
        marker = f'<!-- docker-codex:{key} -->'
        page = 1
        while True:
            issues = api(f'repos/{self.repo}/issues?state=all&per_page=100&page={page}')
            for issue in issues:
                if 'pull_request' not in issue and marker in (issue.get('body') or ''):
                    return issue['number']
            if len(issues) < 100:
                break
            page += 1
        return api(f'repos/{self.repo}/issues', 'POST',
                   {'title': title, 'body': f'{marker}\n\n{body}'})['number']

    def digest(self, ref, missing=False):
        try:
            return run('crane', 'digest', ref)
        except RuntimeError as error:
            # Authentication, network, and rate-limit failures must never permit a rebuild.
            if missing and ('MANIFEST_UNKNOWN' in str(error) or 'NAME_UNKNOWN' in str(error)):
                return None
            raise

    def identity(self, tag):
        release_id(tag)
        if run('git', 'cat-file', '-t', f'refs/tags/{tag}') != 'tag':
            raise ValueError('Release tag must be annotated')
        sha = run('git', 'rev-parse', f'refs/tags/{tag}^{{commit}}')
        version = run('git', 'show', f'{sha}:VERSION')
        if version != tag[1:]:
            raise ValueError('Tag and VERSION disagree')
        dockerfile = subprocess.check_output(['git', 'show', f'{sha}:Dockerfile']).decode()
        return sha, dockerfile

    def record(self, tag, sha, dockerfile, origin, previous, source):
        return dict(schema_version=1, release_id=tag, image_version=tag[1:], git_tag=tag,
                    source_sha=source, release_sha=sha, composition_id=composition(dockerfile),
                    origin=origin, previous_versions=previous, expected_versions=versions(dockerfile),
                    status='reserved', candidate_tag=f'candidate-{tag}', candidate_digest=None,
                    platform_digests={}, checks={}, reports={}, attestation=None, attempts={},
                    failure_count=0, last_error=None, failure_issue=None, published_tags={},
                    created_at=now(), completed_at=None, cancellation=None)

    def verify_identity(self, record):
        sha, dockerfile = self.identity(record['git_tag'])
        if (sha != record['release_sha'] or composition(dockerfile) != record['composition_id']
                or versions(dockerfile) != record['expected_versions']):
            raise ValueError('Tag/source/state identity mismatch')

    def inspect_candidate(self, record, digest, require_evidence=True):
        manifest = json.loads(run('crane', 'manifest', f'{self.image}@{digest}'))
        platforms = {}
        for item in manifest.get('manifests', []):
            platform = item.get('platform', {})
            if platform.get('os') == 'linux' and platform.get('architecture') in ('amd64', 'arm64'):
                arch = platform['architecture']
                if arch in platforms:
                    raise ValueError('Duplicate candidate platform')
                config = json.loads(run('crane', 'config', f"{self.image}@{item['digest']}"))
                if config.get('architecture') != arch or config.get('os') != 'linux':
                    raise ValueError('Candidate config and index platform disagree')
                labels = config.get('config', {}).get('Labels', {})
                if (labels.get('org.opencontainers.image.revision') != record['release_sha']
                        or labels.get('org.opencontainers.image.version') != record['image_version']):
                    raise ValueError('Candidate OCI labels do not match release')
                platforms[arch] = item['digest']
        if set(platforms) != {'amd64', 'arm64'}:
            raise ValueError('Candidate is not a complete AMD64/ARM64 index')
        if require_evidence:
            for platform_digest in platforms.values():
                evidence = [item for item in manifest['manifests'] if
                            item.get('annotations', {}).get('vnd.docker.reference.digest') == platform_digest
                            and item.get('annotations', {}).get('vnd.docker.reference.type') == 'attestation-manifest']
                predicates = set()
                for item in evidence:
                    att = json.loads(run('crane', 'manifest', f"{self.image}@{item['digest']}"))
                    for layer in att.get('layers', []):
                        statement = json.loads(run('crane', 'blob', f"{self.image}@{layer['digest']}"))
                        subjects = statement.get('subject', [])
                        if not any(s.get('digest', {}).get('sha256') == platform_digest.split(':')[1]
                                   for s in subjects):
                            raise ValueError('Build evidence subject mismatch')
                        predicates.add(statement.get('predicateType'))
                if ('https://spdx.dev/Document' not in predicates
                        or predicates.isdisjoint(BUILD_PROVENANCE_TYPES)):
                    raise ValueError('Missing BuildKit provenance or SBOM')
        return platforms

    def initialize(self, tag, digest):
        if self.state['baseline']:
            raise ValueError('Baseline already imported')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Explicit baseline digest required')
        sha, dockerfile = self.identity(tag)
        record = self.record(tag, sha, dockerfile, 'baseline', versions(dockerfile), sha)
        if self.digest(f'{self.image}:{tag[1:]}') != digest:
            raise ValueError('Baseline semver tag/digest mismatch')
        record['platform_digests'] = self.inspect_candidate(record, digest, require_evidence=False)
        record.update(status='imported', candidate_digest=digest)
        major, minor, _ = semver(tag[1:])
        for alias in [tag[1:], f'{major}.{minor}', str(major), 'latest', sha[:7], sha]:
            if self.digest(f'{self.image}:{alias}', missing=True) == digest:
                record['published_tags'][alias] = digest
        self.records[tag] = record
        self.state['baseline'] = tag
        self.store.save()
        summary(f'Imported baseline {tag} at {digest}; new release checks are not claimed.')

    def recover_attempts(self, record):
        changed = False
        for attempt in record['attempts'].values():
            if attempt['status'] == 'running':
                remote = api(f"repos/{self.repo}/actions/runs/{attempt['run_id']}/attempts/{attempt['run_attempt']}")
                if remote['status'] != 'completed':
                    raise ValueError('A previous publishing attempt is still running')
                error = f"Interrupted Actions run {attempt['run_id']}, attempt {attempt['run_attempt']}"
                attempt.update(status='failed', finished_at=now(), error=error)
                record['last_error'] = error
                record['failure_count'] += 1
                changed = True
        if changed:
            self.store.save()
        self.failure_notice(record)

    def failure_notice(self, record):
        if record['failure_count'] >= 3 and not record['failure_issue']:
            try:
                number = self.issue(
                    f"release-failure:{record['release_id']}", f"Release {record['release_id']} needs attention",
                    f"{record['failure_count']} failed attempts. Hourly retries continue while the release is unfinished.\n\n"
                    f"Last error: {record['last_error']}\n\nResume or cancel by exact release ID.")
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as error:
                summary(f"Failure notification will be retried: {error}")
                return
            record['failure_issue'] = number
            self.store.save()

    def dispatch(self, record):
        self.verify_identity(record)
        if record['status'] == 'cancelled':
            raise ValueError('Cancelled releases cannot be resumed')
        api(f'repos/{self.repo}/actions/workflows/build-push.yml/dispatches', 'POST',
            {'ref': record['git_tag'], 'inputs': {'release_id': record['release_id']}})
        summary(f"Dispatched {record['release_id']} ({record['release_sha']}).\n\n"
                f"CLI versions: {json.dumps(record['previous_versions'])} → "
                f"{json.dumps(record['expected_versions'])}. Status: {record['status']}.")

    def control(self, operation, tag='', reason=''):
        if operation not in ('check', 'resume', 'cancel'):
            raise ValueError('Unknown operation')
        if operation != 'check':
            release_id(tag)
            record = self.records[tag]
            self.verify_identity(record)
            if operation == 'resume':
                if record['status'] == 'cancelled':
                    raise ValueError('Cancelled releases cannot be resumed')
                self.recover_attempts(record)
                self.dispatch(record)
                return
            if not reason.strip():
                raise ValueError('Cancellation requires a reason')
            if record['status'] in ('completed', 'imported'):
                raise ValueError('Release is already completed')
            if record['status'] != 'cancelled':
                record.update(status='cancelled', cancellation={
                    'reason': reason, 'actor': os.environ['GITHUB_ACTOR'], 'at': now()})
                self.store.save()
            summary(f"Cancelled {tag}. Existing assignments retained: {record['published_tags']}")
            return
        active = validate_state(self.state)
        if active:
            self.recover_attempts(active)
            self.dispatch(active)
            return
        # Discover manual tags even when a push event was dropped from the concurrency queue.
        baseline = semver(self.state['baseline'][1:])
        pending = [tag for tag in run('git', 'tag', '--list', 'v*').splitlines()
                   if re.fullmatch('v' + STABLE, tag)
                   and semver(tag[1:]) > baseline and tag not in self.records]
        if len(pending) > 1:
            raise ValueError('Multiple unregistered release tags; reconcile explicitly')
        if pending:
            record = self.register_manual(pending[0])
            self.dispatch(record)
            return
        run('git', 'checkout', '-B', self.branch, f'origin/{self.branch}')
        dockerfile = Path('Dockerfile').read_text()
        current = versions(dockerfile)
        image_version = Path('VERSION').read_text().strip()
        semver(image_version)
        last = max((r for r in self.records.values() if r['status'] in ('completed', 'imported')),
                   key=lambda r: semver(r['image_version']))
        cancelled = {r['composition_id'] for r in self.records.values() if r['status'] == 'cancelled'}
        origin = 'manual-composition'
        if composition(dockerfile) == last['composition_id'] or composition(dockerfile) in cancelled:
            responses = {}
            for cli, (package, _) in TOOLS.items():
                url = 'https://registry.npmjs.org/' + urllib.parse.quote(package, safe='')
                with urllib.request.urlopen(url, timeout=60) as response:
                    responses[cli] = json.load(response)
            selected, majors = select(current, responses)
            for package, major, latest in majors:
                self.issue(f'major:{package}:{major}', f'Review {package} major {major}',
                           f'New stable version: {latest}. Accept through a PR changing Dockerfile. '
                           'Closing this issue does not authorize the upgrade.')
            dockerfile = replace_versions(dockerfile, selected)
            origin = 'npm'
        if composition(dockerfile) in cancelled or composition(dockerfile) == last['composition_id']:
            summary('No new eligible composition; no release reserved.')
            return
        highest = max([image_version, *(r['image_version'] for r in self.records.values())], key=semver)
        major, minor, patch = semver(highest)
        tag = f'v{major}.{minor}.{patch + 1}'
        source = run('git', 'rev-parse', 'HEAD')
        Path('Dockerfile').write_text(dockerfile)
        Path('VERSION').write_text(tag[1:] + '\n')
        run('git', 'config', 'user.name', 'github-actions[bot]')
        run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        run('git', 'add', 'Dockerfile', 'VERSION')
        run('git', 'commit', '-m', f'chore: release {tag}')
        sha = run('git', 'rev-parse', 'HEAD')
        run('git', 'tag', '-a', tag, '-m', tag)
        record = self.record(tag, sha, dockerfile, origin, current, source)
        self.records[tag] = record
        state_sha = self.store.commit()
        run('git', 'fetch', str(self.store.root), state_sha)
        # A fast-forward push alone accepts some stale heads (e.g. remote resets); compare too.
        remote_head = run('git', 'ls-remote', 'origin', f'refs/heads/{self.branch}').split()[0]
        if remote_head != source:
            raise ValueError('Default branch changed; reservation aborted')
        run('git', 'push', '--atomic', 'origin', f'HEAD:refs/heads/{self.branch}',
            f'refs/tags/{tag}:refs/tags/{tag}', f'{state_sha}:refs/heads/release-state')
        self.dispatch(record)

    def register_manual(self, tag):
        release_id(tag)
        if tag in self.records:
            return self.records[tag]
        if validate_state(self.state):
            raise ValueError('Another release is active')
        if semver(tag[1:]) <= max(semver(r['image_version']) for r in self.records.values()):
            raise ValueError('Manual tag must use a new version above the reserved numbers')
        sha, dockerfile = self.identity(tag)
        run('git', 'merge-base', '--is-ancestor', sha, f'origin/{self.branch}')
        if composition(dockerfile) in {r['composition_id'] for r in self.records.values()
                                      if r['status'] == 'cancelled'}:
            raise ValueError('Cancelled composition requires a Dockerfile change')
        previous = max(self.records.values(), key=lambda r: semver(r['image_version']))['expected_versions']
        record = self.record(tag, sha, dockerfile, 'manual-tag', previous, sha)
        self.records[tag] = record
        self.store.save()
        return record

    def begin(self, tag):
        release_id(tag)
        if os.environ.get('GITHUB_REF') != f'refs/tags/{tag}':
            raise ValueError('Publication must run on the exact release tag; pass --ref vX.Y.Z')
        record = self.register_manual(tag)
        self.verify_identity(record)
        if record['status'] in ('cancelled', 'imported'):
            raise ValueError(f"Cannot publish a {record['status']} release")
        self.recover_attempts(record)
        attempt_id = f"{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}"
        record['attempts'][attempt_id] = dict(status='running', started_at=now(),
                                            run_id=os.environ['GITHUB_RUN_ID'],
                                            run_attempt=os.environ['GITHUB_RUN_ATTEMPT'])
        self.store.save()

    def prepare(self, tag):
        record = self.records[release_id(tag)]
        self.verify_identity(record)
        if record['status'] in ('cancelled', 'imported'):
            raise ValueError('Release cannot be published')
        output('release_id', tag)
        output('image', self.image)
        run('git', 'checkout', '--detach', record['release_sha'])
        candidate = f"{self.image}:{record['candidate_tag']}"
        digest = self.digest(candidate, missing=True)
        if record['candidate_digest']:
            if digest != record['candidate_digest']:
                raise ValueError('Saved candidate is missing or changed; rebuilding is forbidden')
        elif digest is None:
            run('docker', 'run', '--rm', '-i', '--entrypoint', 'hadolint',
                'hadolint/hadolint:v2.12.0', '--failure-threshold', 'error', '-',
                input=Path('Dockerfile').read_text(), live=True)
            run('docker', 'buildx', 'build', '--platform', 'linux/amd64,linux/arm64', '--push',
                '--provenance=mode=max', '--sbom=true', '--tag', candidate,
                '--build-arg', f"VERSION={record['image_version']}",
                '--build-arg', f"REVISION={record['release_sha']}",
                '--build-arg', f'SOURCE=https://github.com/{self.repo}',
                '--build-arg', f"CREATED={record['created_at']}", '.', timeout=5400, live=True)
            digest = self.digest(candidate)
        platforms = self.inspect_candidate(record, digest, require_evidence=False)
        if record['platform_digests'] and platforms != record['platform_digests']:
            raise ValueError('Candidate platform digests changed')
        record.update(candidate_digest=digest, platform_digests=platforms)
        if record['status'] == 'reserved':
            record['status'] = 'candidate_saved'
        self.store.save()
        # Retain identity even if provenance/SBOM verification fails afterwards.
        self.inspect_candidate(record, digest)
        output('digest', digest)
        if record['status'] == 'completed':
            self.verify_reports(record)
            self.verify_attestation(record)
            self.promote(record, readonly=True)
            self.succeed(record)
            output('done', 'true')
            return
        reports = Path(os.environ['RUNNER_TEMP']) / 'release-reports'
        reports.mkdir(exist_ok=True)
        for arch, platform_digest in platforms.items():
            for cli in TOOLS:
                key = f'{arch}:{cli}'
                check = record['checks'].get(key, {})
                if check.get('digest') == digest and check.get('version') == record['expected_versions'][cli]:
                    continue
                container = f"release-{os.environ['GITHUB_RUN_ID']}-{arch}-{cli}"
                try:
                    actual = cli_version(cli, run('docker', 'run', '--rm', '--name', container,
                        '--platform', f'linux/{arch}', '--entrypoint', f'/usr/local/bin/{cli}',
                        f'{self.image}@{platform_digest}', '--version', timeout=180))
                finally:
                    subprocess.run(['docker', 'rm', '-f', container], capture_output=True, check=False)
                if actual != record['expected_versions'][cli]:
                    raise ValueError(f'{key}: expected {record["expected_versions"][cli]}, got {actual}')
                record['checks'][key] = {'digest': digest, 'version': actual, 'at': now()}
                self.store.save()
            name = f'trivy-{arch}.sarif'
            path = reports / name
            receipt = record['reports'].get(name)
            if receipt:
                self.download_report(record, name, path, receipt['sha256'])
                continue
            # Reconcile an upload that succeeded just before state persistence failed.
            asset = self.asset(record, name)
            if asset:
                self.download_report(record, name, path)
                self.validate_report(path, platform_digest)
            else:
                run('trivy', 'image', '--platform', f'linux/{arch}', '--exit-code', '0',
                    '--severity', 'HIGH,CRITICAL', '--format', 'sarif', '--output', str(path),
                    f'{self.image}@{platform_digest}', timeout=900, live=True)
                document = json.loads(path.read_text())
                document.setdefault('properties', {})['release_digest'] = platform_digest
                path.write_text(json.dumps(document) + '\n')
                self.validate_report(path, platform_digest)
                self.ensure_release(record)
                run('gh', 'release', 'upload', tag, str(path), '--repo', self.repo)
                self.download_report(record, name, path, hashlib.sha256(path.read_bytes()).hexdigest())
            record['reports'][name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                      'digest': digest, 'asset': self.asset(record, name)['url']}
            self.store.save()
        output('reports', str(reports))
        output('attest', 'false' if self.restore_attestation(record) else 'true')

    def restore_attestation(self, record):
        # API storage can succeed before the separate registry push. Reuse only
        # after verifying both copies; an incomplete creation is retried by Actions.
        digest = record['candidate_digest']
        if record['attestation']:
            self.verify_attestation(record)
            return True
        try:
            existing = api(f"repos/{self.repo}/attestations/{digest}")
        except RuntimeError as error:
            # GitHub returns 404 when this valid subject has no attestations yet.
            if 'HTTP 404' in str(error):
                return False
            raise
        if not isinstance(existing.get('attestations'), list):
            raise ValueError('Malformed attestation API response')
        if not existing['attestations']:
            return False
        try:
            self.verify_attestation(record)
        except RuntimeError:
            return False
        record['attestation'] = {'digest': digest, 'verified_at': now()}
        self.store.save()
        return True

    def ensure_release(self, record):
        tag = record['release_id']
        page = 1
        while True:
            releases = api(f'repos/{self.repo}/releases?per_page=100&page={page}')
            if any(r['tag_name'] == tag for r in releases):
                return
            if len(releases) < 100:
                break
            page += 1
        run('gh', 'release', 'create', tag, '--repo', self.repo, '--verify-tag', '--draft',
            '--title', tag, '--notes', 'Reserved release. Publication status is recorded in release-state.')

    def asset(self, record, name):
        # gh release view resolves draft releases by exact tag and paginates assets.
        self.ensure_release(record)
        data = json.loads(run('gh', 'release', 'view', record['release_id'], '--repo', self.repo,
                              '--json', 'assets'))
        return next((item for item in data['assets'] if item['name'] == name), None)

    def download_report(self, record, name, path, checksum=None):
        run('gh', 'release', 'download', record['release_id'], '--repo', self.repo,
            '--pattern', name, '--output', str(path), '--clobber')
        if checksum and hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError(f'Report checksum mismatch: {name}')

    @staticmethod
    def validate_report(path, digest):
        report = json.loads(path.read_text())
        if (report.get('version') != '2.1.0' or not report.get('runs')
                or report.get('properties', {}).get('release_digest') != digest):
            raise ValueError('Malformed scan report or digest mismatch')
        for entry in report['runs']:
            if not entry.get('tool', {}).get('driver', {}).get('name') or not isinstance(entry.get('results'), list):
                raise ValueError('Malformed SARIF run')

    def verify_reports(self, record):
        for arch, digest in record['platform_digests'].items():
            name = f'trivy-{arch}.sarif'
            receipt = record['reports'][name]
            if receipt['digest'] != record['candidate_digest']:
                raise ValueError('Report receipt belongs to another candidate')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / name
                self.download_report(record, name, path, receipt['sha256'])
                self.validate_report(path, digest)
            if receipt.get('sarif_status') != 'complete':
                raise ValueError('Missing successful GitHub Security upload')
            status = api(f"repos/{self.repo}/code-scanning/sarifs/{receipt['sarif_id']}")
            if status['processing_status'] != 'complete':
                raise ValueError('GitHub Security report no longer confirmed')

    def find_sarif(self, record, category):
        ref = urllib.parse.quote(f"refs/tags/{record['git_tag']}", safe='')
        page = 1
        while True:
            analyses = api(f'repos/{self.repo}/code-scanning/analyses?ref={ref}&per_page=100&page={page}')
            for analysis in analyses:
                if (analysis['commit_sha'] == record['release_sha']
                        and analysis.get('category', '').rstrip('/') == category):
                    return analysis['sarif_id']
            if len(analyses) < 100:
                return None
            page += 1

    def upload_sarif(self, record):
        for arch in record['platform_digests']:
            name = f'trivy-{arch}.sarif'
            receipt = record['reports'][name]
            if receipt.get('sarif_status') == 'complete':
                continue
            category = f"trivy-{arch}/{record['candidate_digest']}"
            if not receipt.get('sarif_id'):
                recovered = self.find_sarif(record, category)
                if recovered:
                    receipt['sarif_id'] = recovered
                    self.store.save()
            if not receipt.get('sarif_id'):
                path = Path(os.environ['RUNNER_TEMP']) / 'release-reports' / name
                report = json.loads(path.read_text())
                for entry in report['runs']:
                    entry['automationDetails'] = {'id': category + '/'}
                encoded = base64.b64encode(gzip.compress(json.dumps(report).encode())).decode()
                receipt['sarif_id'] = api(f'repos/{self.repo}/code-scanning/sarifs', 'POST', {
                    'commit_sha': record['release_sha'], 'ref': f"refs/tags/{record['git_tag']}",
                    'sarif': encoded})['id']
                self.store.save()
            for _ in range(60):
                result = api(f"repos/{self.repo}/code-scanning/sarifs/{receipt['sarif_id']}")
                if result['processing_status'] == 'complete':
                    receipt['sarif_status'] = 'complete'
                    self.store.save()
                    break
                if result['processing_status'] == 'failed':
                    receipt.pop('sarif_id')
                    self.store.save()
                    raise ValueError(f'SARIF processing failed: {result}')
                time.sleep(5)
            else:
                raise ValueError('SARIF upload still processing; retry next attempt')

    def verify_attestation(self, record):
        command = ['gh', 'attestation', 'verify', f"oci://{self.image}@{record['candidate_digest']}",
                   '--repo', self.repo, '--signer-workflow', f'{self.repo}/.github/workflows/build-push.yml',
                   '--source-digest', record['release_sha'], '--format', 'json']
        run(*command, timeout=300)
        run(*command, '--bundle-from-oci', timeout=300)

    def finish(self, tag):
        record = self.records[release_id(tag)]
        self.verify_identity(record)
        if record['status'] in TERMINAL:
            raise ValueError('Cannot finish terminal release')
        self.verify_attestation(record)
        record['attestation'] = {'digest': record['candidate_digest'], 'verified_at': now()}
        self.store.save()
        self.upload_sarif(record)
        self.verify_reports(record)
        if any(record['checks'].get(f'{arch}:{cli}', {}).get('digest') != record['candidate_digest']
               or record['checks'].get(f'{arch}:{cli}', {}).get('version') != record['expected_versions'][cli]
               for arch in ('amd64', 'arm64') for cli in TOOLS):
            raise ValueError('Missing CLI verification')
        record['status'] = 'verified'
        self.store.save()
        self.promote(record)
        run('gh', 'release', 'edit', tag, '--repo', self.repo, '--draft=false', '--latest=false')
        record.update(status='completed', completed_at=now())
        self.succeed(record)

    def promote(self, record, readonly=False):
        if record['status'] == 'cancelled':
            raise ValueError('Release cancelled')
        digest = record['candidate_digest']
        if self.digest(f"{self.image}:{record['candidate_tag']}") != digest:
            raise ValueError('Candidate tag no longer matches saved digest')
        self.inspect_candidate(record, digest)
        major, minor, _ = semver(record['image_version'])
        tags = [record['image_version'], record['release_sha'][:7], record['release_sha'],
                f'{major}.{minor}', str(major), 'latest']
        observed = {tag: self.digest(f'{self.image}:{tag}', missing=True) for tag in tags}
        plan = tag_plan(record, self.records, observed)
        if not readonly:
            record['status'] = 'publishing'
            self.store.save()
        for tag, action in plan:
            if action == 'superseded':
                continue
            if action == 'assign':
                if readonly:
                    raise ValueError(f'Completed release tag is missing/changed: {tag}')
                # Entire publication and cancellation share the same workflow concurrency lock.
                # Record intent before the registry action to reconcile a crash after assignment.
                record.setdefault('tag_intents', {})[tag] = digest
                self.store.save()
                run('docker', 'buildx', 'imagetools', 'create', '--tag', f'{self.image}:{tag}',
                    f'{self.image}@{digest}', live=True)
            if self.digest(f'{self.image}:{tag}') != digest:
                raise ValueError(f'Digest changed while assigning {tag}')
            if not readonly:
                record['published_tags'][tag] = digest
                self.store.save()

    def succeed(self, record):
        key = f"{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}"
        record['attempts'][key].update(status='completed', finished_at=now())
        record['last_error'] = None
        self.store.save()
        summary(f"Verified release {record['release_id']}: `{record['candidate_digest']}`\n\n"
                f"CLI: {record['previous_versions']} → {record['expected_versions']}\n\n"
                f"Published tags: {record['published_tags']}")

    def fail(self, tag):
        record = self.records.get(tag)
        if not record:
            return
        key = f"{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}"
        attempt = record['attempts'].get(key)
        if attempt and attempt['status'] == 'running':
            error_file = Path(os.environ['RUNNER_TEMP']) / 'release-error.txt'
            error = error_file.read_text() if error_file.exists() else 'Workflow step failed; see Actions run'
            attempt.update(status='failed', finished_at=now(), error=error)
            record['failure_count'] += 1
            record['last_error'] = error
            self.store.save()
        self.failure_notice(record)


def main():
    command = sys.argv[1]
    tag = os.environ.get('RELEASE_ID', '')
    coordinator = Coordinator(initialize=command == 'initialize')
    if command == 'initialize':
        coordinator.initialize(tag, os.environ.get('BASELINE_DIGEST', ''))
    elif command == 'control':
        coordinator.control(os.environ.get('OPERATION') or 'check', tag, os.environ.get('REASON', ''))
    elif command == 'begin':
        coordinator.begin(tag)
    elif command == 'prepare':
        coordinator.prepare(tag)
    elif command == 'finish':
        coordinator.finish(tag)
    elif command == 'fail':
        coordinator.fail(tag)
    else:
        raise ValueError('Unknown command')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if os.environ.get('RUNNER_TEMP'):
            (Path(os.environ['RUNNER_TEMP']) / 'release-error.txt').write_text(str(error))
        raise
