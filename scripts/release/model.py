"""Pure release policy; no Git, registry, or GitHub side effects."""
import hashlib
import re

TOOLS = {
    'codex': ('@openai/codex', 'CODEX_VERSION'),
    'opencode': ('opencode-ai', 'OPENCODE_VERSION'),
    'claude': ('@anthropic-ai/claude-code', 'CLAUDE_CODE_VERSION'),
}
STABLE = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
TERMINAL = {'completed', 'cancelled', 'imported'}


def semver(value):
    if not isinstance(value, str) or not re.fullmatch(STABLE, value):
        raise ValueError(f'Expected stable SemVer, got {value!r}')
    return tuple(map(int, value.split('.')))


def release_id(value):
    if not isinstance(value, str) or not value.startswith('v'):
        raise ValueError('Expected exact release id vX.Y.Z')
    semver(value[1:])
    return value


def versions(dockerfile):
    result = {}
    for cli, (_, arg) in TOOLS.items():
        matches = re.findall(r'^ARG\s+' + arg + r'(?:=(.*))?\s*$', dockerfile, re.M)
        if len(matches) != 1:
            raise ValueError(f'{arg} must occur exactly once')
        semver(matches[0])
        result[cli] = matches[0]
    return result


def composition(dockerfile):
    return hashlib.sha256(dockerfile.encode()).hexdigest()


def select(current, responses):
    selected, majors = dict(current), []
    for cli, (package, _) in TOOLS.items():
        data = responses[cli]
        latest = data['dist-tags']['latest']
        top = semver(latest)
        published = data['versions']
        if not isinstance(published, dict) or not published or latest not in published:
            raise ValueError(f'Invalid npm versions for {package}')
        if any(not isinstance(v, str) or not isinstance(meta, dict)
               or meta.get('version') != v for v, meta in published.items()):
            raise ValueError(f'Malformed npm metadata for {package}')
        extended = STABLE + r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
        if any(not re.fullmatch(extended, v) for v in published):
            raise ValueError(f'Malformed version in npm metadata for {package}')
        candidates = [v for v in published if re.fullmatch(STABLE, v)
                      and semver(v)[0] == semver(current[cli])[0]]
        selected[cli] = max([current[cli], *candidates], key=semver)
        if top[0] > semver(current[cli])[0]:
            majors.append((package, top[0], latest))
    return selected, majors


def replace_versions(dockerfile, selected):
    versions(dockerfile)
    for cli, (_, arg) in TOOLS.items():
        semver(selected[cli])
        dockerfile = re.sub(r'^ARG ' + arg + r'=.*$',
                            f'ARG {arg}={selected[cli]}', dockerfile, flags=re.M)
    if versions(dockerfile) != selected:
        raise ValueError('Could not replace Dockerfile ARGs')
    return dockerfile


def cli_version(cli, output):
    patterns = {'codex': rf'codex-cli ({STABLE})',
                'opencode': rf'({STABLE})',
                'claude': rf'({STABLE}) \(Claude Code\)'}
    match = re.fullmatch(patterns[cli], output.strip())
    if not match:
        raise ValueError(f'Unexpected {cli} --version output: {output!r}')
    return match[1]


def validate_state(state):
    if state.get('schema_version') != 1 or not state.get('baseline'):
        raise ValueError('Missing baseline or unsupported state schema; explicit import required')
    records = state['releases']
    active = []
    for key, record in records.items():
        if release_id(key) != record['git_tag'] or key != record['release_id']:
            raise ValueError('Release identity conflict')
        if record['schema_version'] != 1 or record['image_version'] != key[1:]:
            raise ValueError('Release schema/version conflict')
        if record['status'] not in TERMINAL | {'reserved', 'candidate_saved', 'verified', 'publishing'}:
            raise ValueError('Unknown release status')
        if record['status'] not in TERMINAL:
            active.append(record)
    if state['baseline'] not in records or len(active) > 1:
        raise ValueError('Missing baseline or multiple active releases')
    return active[0] if active else None


def tag_plan(record, records, observed):
    """Reject unknown ownership; never move immutable tags or regress aliases."""
    version = record['image_version']
    major, minor, _ = semver(version)
    immutable = [version, record['release_sha'][:7], record['release_sha']]
    aliases = [f'{major}.{minor}', str(major), 'latest']
    plan = []
    for tag in immutable + aliases:
        digest = observed.get(tag)
        if digest == record['candidate_digest']:
            plan.append((tag, 'verified'))
        elif digest is None:
            plan.append((tag, 'assign'))
        elif tag in immutable:
            raise ValueError(f'Immutable tag conflict: {tag}')
        else:
            owners = [r for r in records.values() if r.get('candidate_digest') == digest
                      and (tag in r.get('published_tags', {}) or tag in r.get('tag_intents', {}))]
            if not owners:
                raise ValueError(f'Unknown owner of alias {tag}')
            owner = max(owners, key=lambda r: semver(r['image_version']))
            old = semver(owner['image_version'])
            if tag != 'latest' and (old[0] != major or ('.' in tag and old[1] != minor)):
                raise ValueError(f'Wrong version line for alias {tag}')
            plan.append((tag, 'assign' if semver(version) > old else 'superseded'))
    return plan
