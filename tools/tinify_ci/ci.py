"""Trusted CI image optimizer. PR contents are data, never executable inputs."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

MAX_IMAGE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
LEDGER = 'tools/tinify_ci/ledger.json'
MARKER = 'tinify-ci: compressed images v1'
ROOTS = json.loads(Path(__file__).with_name('config.json').read_text())['asset_dirs']


class Failure(Exception):
    pass


def require(value, message):
    if not value:
        raise Failure(message)


def eligible(path):
    parts = path.lower().split('/')
    excluded = ('snapshot', 'golden', 'fixture', 'screenshot', 'reference', 'baseline')
    return (any(path.startswith(root) for root in ROOTS)
            and all(p and p not in ('.', '..') for p in parts)
            and not any(any(word in p for word in excluded) for p in parts)
            and not path.lower().endswith('.9.png')
            and Path(path).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'))


def select(files):
    result = [f for f in files if f['status'] in ('added', 'modified', 'renamed')
              and eligible(f['filename'])]
    require(len(result) <= 30, 'Too many image candidates (maximum 30)')
    return result


def image_info(data):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 20_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with Image.open(io.BytesIO(data)) as image:
                require(image.format in ('PNG', 'JPEG', 'WEBP'), 'Unsupported format')
                require(getattr(image, 'n_frames', 1) == 1, 'Animation rejected')
                require(image.width * image.height <= Image.MAX_IMAGE_PIXELS, 'Too many pixels')
                info = image.format, image.size
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return info
    except Exception:
        raise Failure('Malformed, animated or unsafe image') from None


def validate_output(original, output):
    require(0 < len(output) <= MAX_IMAGE, 'Invalid output size')
    require(image_info(original) == image_info(output), 'Output format/dimensions changed')
    return output if len(output) < len(original) else None


def git_hash(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Failure('GitHub redirect rejected')


class API:
    def __init__(self, repo, token):
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo), 'Invalid repository')
        self.repo, self.token = repo, token
        self.prefix = '/repos/' + repo
        self.deadline = time.monotonic() + 600
        self.trees = {}

    def remaining(self):
        left = self.deadline - time.monotonic()
        require(left > 0, 'Overall ten minute deadline exceeded')
        return left

    def request(self, method, path, body=None):
        require(path.startswith(self.prefix) and not path.startswith('//'), 'Invalid API path')
        request = urllib.request.Request('https://api.github.com' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}, method=method)
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=min(20, self.remaining())) as response:
                data = response.read(12 * 1024 * 1024 + 1)
            require(len(data) <= 12 * 1024 * 1024, 'GitHub response too large')
            self.remaining()
            return json.loads(data)
        except Exception:
            raise Failure('GitHub API request failed (details suppressed)') from None

    def get(self, path):
        return self.request('GET', self.prefix + path)

    def write(self, path, body, method='POST'):
        return self.request(method, self.prefix + path, body)

    def blob(self, sha, limit=MAX_IMAGE):
        require(re.fullmatch('[0-9a-f]{40}', sha), 'Invalid blob SHA')
        obj = self.get('/git/blobs/' + sha)
        require(obj['encoding'] == 'base64' and obj['size'] <= limit, 'Invalid blob size/encoding')
        try:
            data = base64.b64decode(''.join(obj['content'].split()), validate=True)
        except Exception:
            raise Failure('Invalid blob encoding') from None
        require(len(data) == obj['size'] and len(data) <= limit
                and git_hash(data) == sha and obj['sha'] == sha, 'Blob SHA/size mismatch')
        return data

    def entry(self, tree, path):
        parts = path.split('/')
        for index, part in enumerate(parts):
            if tree not in self.trees:
                obj = self.get('/git/trees/' + tree)
                require(not obj.get('truncated'), 'Truncated tree')
                self.trees[tree] = obj['tree']
            entries = [e for e in self.trees[tree] if e['path'] == part]
            require(len(entries) == 1, 'Missing/ambiguous tree entry')
            entry = entries[0]
            if index < len(parts) - 1:
                require(entry['type'] == 'tree' and entry['mode'] == '040000', 'Unsafe ancestor')
                tree = entry['sha']
            else:
                require(entry['type'] == 'blob' and entry['mode'] in ('100644', '100755'),
                        'Symlink/submodule/nonregular file rejected')
                return entry

    def files(self, number, count):
        require(0 <= count <= 3000, 'PR file limit exceeded')
        files = []
        for page in range(1, (count + 99) // 100 + 1):
            batch = self.get(f'/pulls/{number}/files?per_page=100&page={page}')
            require(isinstance(batch, list) and len(batch) <= 100, 'Invalid files page')
            files.extend(batch)
        require(len(files) == count and len({f['filename'] for f in files}) == count,
                'PR files count mismatch')
        return files


def check_pr(pr, repo, default, fixed=None):
    require(pr['state'] == 'open' and pr['user']['login'] == 'Armenm'
            and pr['head']['repo']['full_name'] == repo
            and pr['base']['repo']['full_name'] == repo
            and pr['base']['ref'] == default and pr['head']['ref'] != default,
            'Only open Armenm same-repository PRs targeting default branch are allowed')
    pair = pr['head']['sha'], pr['base']['sha']
    require(all(re.fullmatch('[0-9a-f]{40}', sha) for sha in pair), 'Invalid PR SHA')
    require(fixed is None or pair == fixed, 'PR head/base race detected')
    return pair


def authenticated_bot(commit):
    info = commit.get('commit', {})
    actor = commit.get('author') or {}
    verification = info.get('verification', {})
    return (actor.get('login') == 'github-actions[bot]' and actor.get('id') == 41898282
            and info.get('author', {}).get('email') ==
            '41898282+github-actions[bot]@users.noreply.github.com'
            and info.get('committer', {}).get('email') == 'noreply@github.com'
            and verification.get('verified') is True and verification.get('reason') == 'valid'
            and info.get('message') == MARKER and len(commit.get('parents', [])) == 1)


def parse_ledger(data):
    try:
        obj = json.loads(data)
        require(set(obj) == {'version', 'sha256'} and obj['version'] == 1
                and isinstance(obj['sha256'], list) and len(obj['sha256']) <= 100000
                and all(isinstance(s, str) and re.fullmatch('[0-9a-f]{64}', s)
                        for s in obj['sha256']), 'Invalid ledger')
        return set(obj['sha256'])
    except (ValueError, TypeError):
        raise Failure('Invalid ledger') from None


def ledger_at(api, tree):
    return parse_ledger(api.blob(api.entry(tree, LEDGER)['sha'], MAX_IMAGE))


def history(api, base, head):
    comparison = api.get(f'/compare/{base}...{head}?per_page=100')
    commits = comparison['commits']
    require(comparison['total_commits'] <= 100 and len(commits) == comparison['total_commits'],
            'History exceeds authentication limit (100 commits)')
    accepted, bot_head = set(), False
    for brief in commits:
        info = brief['commit']
        actor = brief.get('author') or {}
        if info['message'].startswith('tinify-ci:') or actor.get('login') == 'github-actions[bot]':
            commit = api.get('/commits/' + brief['sha'] + '?per_page=100')
            require(authenticated_bot(commit), 'Cannot authenticate bot history')
            files = commit['files']
            require(1 < len(files) <= 31 and any(f['filename'] == LEDGER for f in files)
                    and all((f['filename'] == LEDGER or eligible(f['filename']))
                            and f['status'] in ('added', 'modified') for f in files),
                    'Bot history contains non-compression changes')
            tree = commit['commit']['tree']['sha']
            parent = api.get('/git/commits/' + commit['parents'][0]['sha'])['tree']['sha']
            values = ledger_at(api, tree)
            for f in files:
                if f['filename'] != LEDGER:
                    old = api.blob(api.entry(parent, f['filename'])['sha'])
                    new = api.blob(api.entry(tree, f['filename'])['sha'])
                    require(validate_output(old, new) is not None, 'Invalid historical compression')
                    require({hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()}
                            <= values, 'Incomplete bot ledger')
            accepted.update(values)
            bot_head = brief['sha'] == head
    return accepted, bot_head


def bounded_response(response, *args, **kwargs):
    """Requests hook runs before eager buffering, including error responses."""
    try:
        require(not 300 <= response.status_code < 400, 'Tinify redirect rejected')
        data = bytearray()
        for chunk in response.iter_content(chunk_size=min(65536, MAX_IMAGE + 1)):
            require(len(data) + len(chunk) <= MAX_IMAGE, 'Tinify response too large')
            data.extend(chunk)
        # Requests' pinned Response.content API consumes this cache after hooks run.
        response._content = bytes(data)
        response._content_consumed = True
        return response
    finally:
        response.close()


def worker(source, output):
    import tinify
    tinify.key = os.environ['TINYPNG_API_KEY']
    session = tinify.get_client().session
    original_request = session.request
    def request(method, url, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'https' and parsed.netloc == 'api.tinify.com',
                'Unexpected Tinify origin')
        return original_request(method, url, timeout=20, allow_redirects=False, **kwargs)
    session.request = request
    session.hooks['response'] = [bounded_response]
    result = tinify.from_buffer(Path(source).read_bytes()).to_buffer()
    require(0 < len(result) <= MAX_IMAGE, 'Invalid SDK output size')
    Path(output).write_bytes(result)


def compress(data, key, timeout):
    with tempfile.TemporaryDirectory(prefix='tinify-ci-', dir=os.environ.get('RUNNER_TEMP', str(Path(__file__).parent))) as directory:
        source, output = Path(directory) / 'input', Path(directory) / 'output'
        source.write_bytes(data)
        # Child receives no GitHub credentials; never print SDK exceptions or output.
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'TMPDIR')}
        env['TINYPNG_API_KEY'] = key
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker',
                            str(source), str(output)], env=env, check=True,
                           timeout=min(60, timeout), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with output.open('rb') as stream:
                result = stream.read(MAX_IMAGE + 1)
            require(0 < len(result) <= MAX_IMAGE, 'Invalid SDK output size')
            return result
        except Exception:
            raise Failure('Tinify compression failed; ACTION REQUIRED (SDK/quota/timeout/output)') from None


def prepare(api, tree, files, accepted, dry_run, key):
    changes, ledger, total = [], set(accepted), 0
    inputs = []
    for file in select(files):
        entry = api.entry(tree, file['filename'])
        require(entry['sha'] == file['sha'], 'PR file/tree SHA mismatch')
        data = api.blob(entry['sha'])
        total += len(data)
        require(total <= MAX_TOTAL, 'Image batch exceeds 32MB')
        image_info(data)
        inputs.append((file['filename'], entry['mode'], data))
    # Reserve one input and at most one new output hash per distinct upload.
    # Output hashes are unknowable before paid requests; never evict trusted hashes.
    pending = {hashlib.sha256(data).hexdigest() for _, _, data in inputs} - ledger
    require(dry_run or len(ledger) + 2 * len(pending) <= 100000,
            'ACTION REQUIRED: ledger capacity requires owner-approved maintenance on the '
            'trusted base before compression; preserve deduplication history, do not silently prune')
    results = {}
    for path, mode, data in inputs:
        digest = hashlib.sha256(data).hexdigest()
        if digest in results:
            if results[digest] is not None:
                changes.append((path, mode, results[digest]))
            continue
        if digest in ledger or dry_run:
            continue
        require(key, 'ACTION REQUIRED: set TINYPNG_API_KEY for eligible images')
        result = validate_output(data, compress(data, key, api.remaining()))
        results[digest] = result
        ledger.add(digest)
        if result is not None:
            ledger.add(hashlib.sha256(result).hexdigest())
            changes.append((path, mode, result))
    api.remaining()
    return changes, ledger


def handoff(sha):
    return (f'ACTION REQUIRED: optimized commit {sha}. Manually approve the queued existing '
            f'normal CI workflow for this exact SHA: {sha}. If no run is queued, arrange '
            'normal CI for this exact SHA. Check its required results before merging. '
            'This optimizer does not certify CI success.')


def report(message):
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(message + '\n')


def publish(api, number, pr, default, fixed, tree, changes, ledger):
    check_pr(api.get(f'/pulls/{number}'), api.repo, default, fixed)
    entries = []
    ledger_data = (json.dumps({'version': 1, 'sha256': sorted(ledger)}) + '\n').encode()
    parse_ledger(ledger_data)  # Enforce the reader contract before any GitHub object/ref write.
    payloads = changes + [(LEDGER, '100644', ledger_data)]
    for path, mode, data in payloads:
        blob = api.write('/git/blobs', {'encoding': 'base64',
                                       'content': base64.b64encode(data).decode()})
        require(blob['sha'] == git_hash(data), 'Created blob SHA mismatch')
        entries.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': blob['sha']})
    new_tree = api.write('/git/trees', {'base_tree': tree, 'tree': entries})['sha']
    # GitHub App signing requires omission of author, committer AND signature.
    sha = api.write('/git/commits', {'message': MARKER, 'tree': new_tree,
                                   'parents': [fixed[0]]})['sha']
    require(re.fullmatch('[0-9a-f]{40}', sha), 'Invalid created SHA')
    created = api.get('/commits/' + sha)
    require(authenticated_bot(created), 'Cannot authenticate created bot commit; branch untouched')
    require(created['sha'] == sha and created['parents'][0]['sha'] == fixed[0]
            and created['commit']['tree']['sha'] == new_tree, 'Created commit mismatch')
    live = api.get(f'/pulls/{number}')
    check_pr(live, api.repo, default, fixed)
    require(live['head']['ref'] == pr['head']['ref'], 'PR branch race detected')
    branch = urllib.parse.quote(pr['head']['ref'], safe='')
    # Persist the known SHA BEFORE the request: a lost response may still mean success.
    report(f'Attempting publication of {sha}. If update/readback fails, outcome is uncertain; '
           f'inspect the live branch and never force retry. {handoff(sha)}')
    result = api.write('/git/refs/heads/' + branch, {'sha': sha, 'force': False}, 'PATCH')
    require(result['object']['sha'] == sha
            and api.get('/git/ref/heads/' + branch)['object']['sha'] == sha,
            'Branch update/readback mismatch; inspect branch before retrying')
    return sha


def run(api, number, trusted_sha, dry_run, key):
    default = api.get('')['default_branch']
    branch = urllib.parse.quote(default, safe='')
    require(api.get('/branches/' + branch)['commit']['sha'] == trusted_sha,
            'Trusted checkout is no longer current default branch')
    pr = api.get(f'/pulls/{number}')
    fixed = check_pr(pr, api.repo, default)
    require(fixed[1] == trusted_sha, 'PR base differs from trusted checkout')
    files = select(api.files(number, pr['changed_files']))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'candidates={len(files)}\n')
    if not files:
        # A compression can restore the base image exactly, removing it from the
        # PR diff. Keep the approval handoff even in dependency-free preflight.
        commit = api.get('/commits/' + fixed[0] + '?per_page=100')
        bot_head = authenticated_bot(commit)
        if bot_head:
            changed = commit['files']
            require(1 < len(changed) <= 31 and any(f['filename'] == LEDGER for f in changed)
                    and all((f['filename'] == LEDGER or eligible(f['filename']))
                            and f['status'] in ('added', 'modified') for f in changed),
                    'Bot HEAD contains non-compression changes')
        check_pr(api.get(f'/pulls/{number}'), api.repo, default, fixed)
        return fixed[0] if bot_head else None
    tree = api.get('/git/commits/' + fixed[0])['tree']['sha']
    if dry_run:
        # Stdlib preflight: validate selection, exact paths, blob hashes and size budget.
        total = 0
        for file in files:
            entry = api.entry(tree, file['filename'])
            require(entry['sha'] == file['sha'], 'PR file/tree SHA mismatch')
            total += len(api.blob(entry['sha']))
            require(total <= MAX_TOTAL, 'Image batch exceeds 32MB')
        check_pr(api.get(f'/pulls/{number}'), api.repo, default, fixed)
        return None
    base_tree = api.get('/git/commits/' + fixed[1])['tree']['sha']
    accepted = ledger_at(api, base_tree)
    previous, bot_head = history(api, fixed[1], fixed[0])
    accepted.update(previous)
    if bot_head:
        check_pr(api.get(f'/pulls/{number}'), api.repo, default, fixed)
        return fixed[0]  # Reruns must retain the nonzero exact-SHA CI approval handoff.
    changes, ledger = prepare(api, tree, files, accepted, False, key)
    check_pr(api.get(f'/pulls/{number}'), api.repo, default, fixed)
    if changes:
        return publish(api, number, pr, default, fixed, tree, changes, ledger)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--worker', nargs=2, metavar=('INPUT', 'OUTPUT'))
    args = parser.parse_args()
    try:
        if args.worker:
            worker(*args.worker)
            return 0
        require(args.pr is not None and args.pr > 0, 'A positive --pr is required')
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        name = os.environ['GITHUB_EVENT_NAME']
        if name == 'pull_request_target':
            require(event['action'] in ('opened', 'synchronize', 'reopened'),
                    'Unsupported PR action')
            if event.get('sender', {}).get('type') == 'Bot':
                return 0
            require(event['number'] == args.pr, 'PR event number mismatch')
        elif name == 'workflow_dispatch':
            require(os.environ['GITHUB_REF'] ==
                    'refs/heads/' + event['repository']['default_branch'],
                    'Dispatch must run on default branch')
        else:
            raise Failure('Unsupported workflow event')
        api = API(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
        sha = run(api, args.pr, os.environ['GITHUB_SHA'], args.dry_run,
                  os.environ.get('TINYPNG_API_KEY', ''))
        message = handoff(sha) if sha else ('Dry run: selection validated; no uploads or writes.'
                                           if args.dry_run else 'No image changes to publish.')
        report(message)
        return 2 if sha else 0
    except Exception as error:
        print(str(error) if isinstance(error, Failure) else
              'Tinify CI failed safely (details suppressed)', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
