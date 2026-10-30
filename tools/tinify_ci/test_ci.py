import base64
import hashlib
import io
import json
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from tools.tinify_ci import ci


# Contract tests use fixed fixture roots, independent of each repo's trusted config.
ROOT_PATCH = patch.object(ci, 'ROOTS', ['public/assets/', 'public/demo/'])


def setUpModule():
    ROOT_PATCH.start()


def tearDownModule():
    ROOT_PATCH.stop()


def picture(fmt='PNG', size=(3, 4), **kwargs):
    out = io.BytesIO()
    Image.new('RGB', size, 'red').save(out, format=fmt, **kwargs)
    return out.getvalue()


class SelectionTests(unittest.TestCase):
    def test_paths_and_status(self):
        for path in ['public/assets/a b.png', 'public/demo/a\nb.JPG', 'public/assets/x.webp']:
            self.assertTrue(ci.eligible(path))
        for path in ['other/a.png', 'public/assets/../a.png', 'public/assets/a.9.png',
                     'public/assets/snapshots/a.png', 'public/assets/__Snapshots__/a.png',
                     'public/assets/goldens/a.png', 'public/assets/fixtures/a.png',
                     'public/assets/screenshot-refs/a.png', '/public/assets/a.png']:
            self.assertFalse(ci.eligible(path), path)
        files = [{'filename': 'public/demo/x.png', 'status': s} for s in
                 ['added', 'modified', 'renamed', 'removed', 'unchanged']]
        self.assertEqual(len(ci.select(files)), 3)
        with self.assertRaises(ci.Failure):
            ci.select(files[:1] * 31)

    def test_baselines_are_not_production_images(self):
        for path in ['public/assets/baselines/a.png', 'public/demo/baseline/a.webp']:
            self.assertFalse(ci.eligible(path), path)

    def test_real_image_validation(self):
        data = picture()
        self.assertEqual(ci.image_info(data), ('PNG', (3, 4)))
        for bad in [b'junk', data[:25]]:
            with self.assertRaises(ci.Failure):
                ci.image_info(bad)
        animated = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(animated, format='PNG', save_all=True,
            append_images=[Image.new('RGB', (3, 4), 'blue')])
        with self.assertRaises(ci.Failure):
            ci.image_info(animated.getvalue())
        with self.assertRaises(ci.Failure):
            ci.validate_output(data, picture('JPEG'))
        with self.assertRaises(ci.Failure):
            ci.validate_output(data, picture(size=(5, 5)))
        self.assertEqual(ci.validate_output(data, data), None)
        large = picture(compress_level=0)
        self.assertEqual(ci.validate_output(large, data), data)


class APITests(unittest.TestCase):
    def test_blob_hash_and_limit(self):
        raw = picture()
        sha = ci.git_hash(raw)
        api = ci.API('owner/repo', 'never-log-this')
        with patch.object(api, 'request', return_value={'encoding': 'base64', 'size': len(raw),
                'sha': sha, 'content': base64.b64encode(raw).decode()}):
            self.assertEqual(api.blob(sha), raw)
            with self.assertRaises(ci.Failure):
                api.blob('0' * 40)
        with patch.object(api, 'request', return_value={'encoding': 'base64',
                'size': ci.MAX_IMAGE + 1, 'sha': sha, 'content': ''}):
            with self.assertRaises(ci.Failure):
                api.blob(sha)

    def test_exact_tree_ancestors(self):
        api = ci.API('owner/repo', 'fake')
        trees = {'root': [{'path': 'public', 'type': 'tree', 'mode': '040000', 'sha': 'p'}],
                 'p': [{'path': 'assets', 'type': 'tree', 'mode': '040000', 'sha': 'a'}],
                 'a': [{'path': 'a\nb.png', 'type': 'blob', 'mode': '100644', 'sha': 'b'}]}
        with patch.object(api, 'request', side_effect=lambda method, path, body=None:
                {'tree': trees[path.rsplit('/', 1)[-1]], 'truncated': False}) as request:
            self.assertEqual(api.entry('root', 'public/assets/a\nb.png')['sha'], 'b')
            self.assertFalse(any('recursive' in c.args[1] for c in request.call_args_list))
            api.trees.clear()
            trees['p'][0].update(type='blob', mode='120000')
            with self.assertRaises(ci.Failure):
                api.entry('root', 'public/assets/a\nb.png')

    def test_transport_sanitized_and_bounded(self):
        api = ci.API('owner/repo', 'secret')
        with patch('urllib.request.OpenerDirector.open', side_effect=RuntimeError('secret')):
            with self.assertRaises(ci.Failure) as error:
                api.request('GET', '/repos/owner/repo')
            self.assertNotIn('secret', str(error.exception))
        with self.assertRaises(ci.Failure):
            api.request('GET', 'https://evil.invalid/')


def pr():
    return {'state': 'open', 'user': {'login': 'Armenm'}, 'changed_files': 1,
            'head': {'sha': 'a' * 40, 'ref': 'feature', 'repo': {'full_name': 'Armenm/repo'}},
            'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'full_name': 'Armenm/repo'}}}


class TrustTests(unittest.TestCase):
    def test_live_pr_contract_and_races(self):
        import copy
        original = pr()
        self.assertEqual(ci.check_pr(original, 'Armenm/repo', 'main'), ('a' * 40, 'b' * 40))
        for mutate in [lambda p: p.update(state='closed'),
                       lambda p: p['user'].update(login='someone'),
                       lambda p: p['head']['repo'].update(full_name='fork/repo'),
                       lambda p: p['base'].update(ref='other'),
                       lambda p: p['head'].update(ref='main')]:
            bad = copy.deepcopy(original)
            mutate(bad)
            with self.assertRaises(ci.Failure):
                ci.check_pr(bad, 'Armenm/repo', 'main')
        for side in ['head', 'base']:
            bad = copy.deepcopy(original)
            bad[side]['sha'] = 'c' * 40
            with self.assertRaises(ci.Failure):
                ci.check_pr(bad, 'Armenm/repo', 'main', ('a' * 40, 'b' * 40))

    def test_bot_authentication_and_ledger(self):
        import copy
        bot = {'committer': {'login': 'web-flow', 'id': 19864447},
               'author': {'login': 'github-actions[bot]', 'id': 41898282},
               'commit': {'message': ci.MARKER, 'committer': {'email': 'noreply@github.com'},
                   'author': {'email': '41898282+github-actions[bot]@users.noreply.github.com'},
                   'verification': {'verified': True, 'reason': 'valid'}},
               'parents': [{'sha': 'a' * 40}]}
        self.assertTrue(ci.authenticated_bot(bot))
        for mutate in [lambda b: b['commit']['verification'].update(verified=False),
                       lambda b: b['author'].update(login='Armenm'),
                       lambda b: b['author'].update(id=1),
                       lambda b: b['commit']['author'].update(email='forged@example.com'),
                       lambda b: b['commit']['committer'].update(email='forged@example.com'),
                       lambda b: b['commit']['verification'].update(reason='unsigned'),
                       lambda b: b['commit'].update(message='forged')]:
            bad = copy.deepcopy(bot)
            mutate(bad)
            self.assertFalse(ci.authenticated_bot(bad))
        digest = hashlib.sha256(picture()).hexdigest()
        self.assertEqual(ci.parse_ledger(json.dumps({'version': 1, 'sha256': [digest]}).encode()), {digest})
        for bad in [b'{}', b'{"version":2,"sha256":[]}', b'{"version":1,"sha256":["bad"]}']:
            with self.assertRaises(ci.Failure):
                ci.parse_ledger(bad)

    def test_history_rejects_forged_bot_or_code_changes(self):
        api = ci.API('Armenm/repo', 'fake')
        commit = {'sha': 'c' * 40, 'committer': {'login': 'github-actions[bot]'},
                  'commit': {'message': ci.MARKER}}
        with patch.object(api, 'get', return_value={'total_commits': 1, 'commits': [commit]}):
            with self.assertRaises(ci.Failure):
                ci.history(api, 'b' * 40, 'a' * 40)
        with patch.object(api, 'get', return_value={'total_commits': 101, 'commits': []}):
            with self.assertRaises(ci.Failure):
                ci.history(api, 'b' * 40, 'a' * 40)

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.api = ci.API('Armenm/repo', 'fake')
        self.raw = picture(compress_level=0)
        self.sha = ci.git_hash(self.raw)
        self.files = [{'filename': 'public/assets/a b\n.png', 'status': 'added', 'sha': self.sha}]

    def test_duplicate_inputs_apply_saved_output_to_every_path(self):
        files = self.files + [dict(self.files[0], filename='public/demo/b.png')]
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw), \
             patch.object(ci, 'compress', return_value=picture()) as compressor:
            changes, ledger = ci.prepare(self.api, 'tree', files, set(), False, 'fake')
        self.assertEqual([change[0] for change in changes], [f['filename'] for f in files])
        self.assertTrue(all(change[2] == picture() for change in changes))
        self.assertEqual(compressor.call_count, 1)
        self.assertIn(hashlib.sha256(self.raw).hexdigest(), ledger)
        self.assertIn(hashlib.sha256(picture()).hexdigest(), ledger)

    def test_capacity_fails_before_upload_without_pruning(self):
        accepted = {f'{i:064x}' for i in range(100000)}
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw), \
             patch.object(ci, 'compress', return_value=picture()) as compressor, \
             patch.object(self.api, 'write') as write:
            with self.assertRaisesRegex(ci.Failure, 'ACTION REQUIRED.*ledger.*maintenance'):
                ci.prepare(self.api, 'tree', self.files, accepted, False, 'fake')
            compressor.assert_not_called()
            write.assert_not_called()
        self.assertEqual(len(accepted), 100000)

    def test_capacity_reserves_per_unique_upload_for_the_whole_batch(self):
        files = self.files + [dict(self.files[0], filename='public/demo/b.png')]
        accepted = {f'{i:064x}' for i in range(99998)}
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw), \
             patch.object(ci, 'compress', return_value=picture()) as compressor:
            changes, ledger = ci.prepare(self.api, 'tree', files, accepted, False, 'fake')
            self.assertEqual(len(changes), 2)
            self.assertEqual(len(ledger), 100000)
            self.assertEqual(ci.parse_ledger(json.dumps({'version': 1, 'sha256': sorted(ledger)})), ledger)
            compressor.assert_called_once()
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', side_effect=[self.raw, picture(size=(4, 4))]), \
             patch.object(ci, 'compress') as compressor:
            with self.assertRaisesRegex(ci.Failure, 'ACTION REQUIRED.*ledger.*maintenance'):
                ci.prepare(self.api, 'tree', files, accepted, False, 'fake')
            compressor.assert_not_called()

    def test_duplicate_no_savings_uploads_once_without_changes(self):
        files = self.files + [dict(self.files[0], filename='public/demo/b.png')]
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw), \
             patch.object(ci, 'compress', return_value=self.raw) as compressor:
            changes, ledger = ci.prepare(self.api, 'tree', files, set(), False, 'fake')
        self.assertEqual(changes, [])
        self.assertEqual(ledger, {hashlib.sha256(self.raw).hexdigest()})
        compressor.assert_called_once()

    def test_prepare_no_savings_repeat_rename_and_missing_key(self):
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw):
            with self.assertRaisesRegex(ci.Failure, 'ACTION REQUIRED'):
                ci.prepare(self.api, 'tree', self.files, set(), False, '')
            with patch.object(ci, 'compress', return_value=self.raw) as compressor:
                changes, ledger = ci.prepare(self.api, 'tree', self.files, set(), False, 'fake')
                self.assertEqual(changes, [])
                self.assertIn(hashlib.sha256(self.raw).hexdigest(), ledger)
                self.files[0]['filename'] = 'public/demo/renamed.png'
                self.assertEqual(ci.prepare(self.api, 'tree', self.files, ledger, False, '')[0], [])
                self.assertEqual(compressor.call_count, 1)
            with patch.object(ci, 'compress') as compressor:
                self.assertEqual(ci.prepare(self.api, 'tree', self.files, set(), True, '')[0], [])
                compressor.assert_not_called()

    def test_whole_batch_prepares_before_writes_and_failures(self):
        files = self.files + [dict(self.files[0], filename='public/demo/b.png')]
        with patch.object(self.api, 'entry', return_value={'sha': self.sha, 'mode': '100644'}), \
             patch.object(self.api, 'blob', return_value=self.raw), \
             patch.object(self.api, 'write') as write:
            for error in [ci.Failure('quota'), ci.Failure('SDK'), ci.Failure('timeout')]:
                with patch.object(self.api, 'blob', side_effect=[self.raw, picture(size=(4, 4), compress_level=0)]), \
                     patch.object(ci, 'compress', side_effect=[picture(), error]):
                    with self.assertRaises(ci.Failure):
                        ci.prepare(self.api, 'tree', files, set(), False, 'fake')
                    write.assert_not_called()
            with patch.object(ci, 'compress', return_value=b'bad'):
                with self.assertRaises(ci.Failure):
                    ci.prepare(self.api, 'tree', files[:1], set(), False, 'fake')
            with patch.object(self.api, 'blob', side_effect=[self.raw, ci.Failure('API')]), \
                 patch.object(ci, 'compress', return_value=picture()):
                with self.assertRaises(ci.Failure):
                    ci.prepare(self.api, 'tree', files, set(), False, 'fake')
                write.assert_not_called()

    def test_sdk_subprocess_timeout_and_sanitization(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 60)) as run:
            with self.assertRaisesRegex(ci.Failure, 'compression failed'):
                ci.compress(self.raw, 'fake', 60)
            self.assertLessEqual(run.call_args.kwargs['timeout'], 60)
            self.assertNotIn('GITHUB_TOKEN', run.call_args.kwargs['env'])

    def test_worker_official_sdk_boundary(self):
        import requests
        def send(adapter, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.headers['location'] = 'https://api.tinify.com/output/test'
            response.raw = io.BytesIO(picture() if request.method == 'GET' else b'{}')
            self.assertEqual(kwargs['timeout'], 20)
            return response
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source, output = Path(directory) / 'in', Path(directory) / 'out'
            source.write_bytes(self.raw)
            with patch('requests.adapters.HTTPAdapter.send', new=send), \
                 patch.dict('os.environ', {'TINYPNG_API_KEY': 'fake'}):
                ci.worker(source, output)
                self.assertEqual(output.read_bytes(), picture())

class PublishTests(unittest.TestCase):
    def test_invalid_generated_ledger_never_writes_objects(self):
        api = ci.API('Armenm/repo', 'fake')
        for ledger in [{f'{i:064x}' for i in range(100001)}, {'invalid'}]:
            with patch.object(api, 'get', return_value=pr()), patch.object(api, 'write') as write:
                with self.assertRaisesRegex(ci.Failure, 'Invalid ledger'):
                    ci.publish(api, 1, pr(), 'main', ('a'*40, 'b'*40), 'tree',
                               [('public/demo/a.png', '100644', picture())], ledger)
                write.assert_not_called()

    def test_tree_parent_ref_and_exact_sha_handoff(self):
        api = ci.API('Armenm/repo', 'fake')
        fixed = ('a' * 40, 'b' * 40)
        output = 'c' * 40
        writes = []
        def write(path, body, method='POST'):
            writes.append((path, body, method))
            if path == '/git/blobs':
                return {'sha': ci.git_hash(base64.b64decode(body['content']))}
            return {'sha': output} if path != '/git/refs/heads/feature' else {'object': {'sha': output}}
        with patch.object(api, 'get', side_effect=[pr(), signed_commit(output), pr(), {'object': {'sha': output}}]), \
             patch.object(api, 'write', side_effect=write):
            result = ci.publish(api, 1, pr(), 'main', fixed, 'tree',
                                [('public/demo/a b\n.png', '100644', picture())], {'0' * 64})
        self.assertEqual(result, output)
        tree = next(b for p, b, m in writes if p == '/git/trees')
        self.assertEqual(tree['base_tree'], 'tree')
        self.assertEqual(tree['tree'][0]['path'], 'public/demo/a b\n.png')
        self.assertEqual(tree['tree'][1]['path'], ci.LEDGER)
        commit = next(b for p, b, m in writes if p == '/git/commits')
        self.assertEqual(commit['parents'], [fixed[0]])
        self.assertTrue({'author', 'committer', 'signature'}.isdisjoint(commit))
        self.assertEqual(writes[-1][1], {'sha': output, 'force': False})
        self.assertIn('ACTION REQUIRED', ci.handoff(output))
        self.assertIn(output, ci.handoff(output))
        self.assertIn('approve', ci.handoff(output))

    def test_publish_race_and_mid_write_failure_leave_ref_untouched(self):
        api = ci.API('Armenm/repo', 'fake')
        bad = pr()
        bad['base']['sha'] = 'c' * 40
        for replies in [[bad], [pr(), signed_commit('c'*40), bad]]:
            with patch.object(api, 'get', side_effect=replies), \
                 patch.object(api, 'write', side_effect=lambda p, b, method='POST':
                    {'sha': ci.git_hash(base64.b64decode(b['content']))} if p == '/git/blobs' else {'sha': 'c'*40}) as write:
                with self.assertRaises(ci.Failure):
                    ci.publish(api, 1, pr(), 'main', ('a'*40, 'b'*40), 'tree',
                               [('public/demo/a.png', '100644', picture())], set())
                self.assertFalse(any('/refs/' in c.args[0] for c in write.call_args_list))
        for fail_at in range(4):
            calls = []
            def failing(path, body, method='POST'):
                calls.append(path)
                if len(calls) == fail_at + 1:
                    raise ci.Failure('API')
                return {'sha': ci.git_hash(base64.b64decode(body['content']))} if path == '/git/blobs' else {'sha': 'c'*40}
            with patch.object(api, 'get', return_value=pr()), patch.object(api, 'write', side_effect=failing):
                with self.assertRaises(ci.Failure):
                    ci.publish(api, 1, pr(), 'main', ('a'*40, 'b'*40), 'tree',
                               [('public/demo/a.png', '100644', picture())], set())
                self.assertFalse(any('/refs/' in p for p in calls))

    def test_no_candidates_and_no_savings_never_write_or_load_sdk(self):
        for files in [[], [{'filename': 'public/demo/a.png', 'status': 'modified', 'sha': 'd'*40}]]:
            api = ci.API('Armenm/repo', 'fake')
            p = pr()
            p['changed_files'] = len(files)
            def get(path):
                if path == '': return {'default_branch': 'main'}
                if path == '/branches/main': return {'commit': {'sha': 'b'*40}}
                if path == '/pulls/1': return p
                if path.startswith('/commits/'): return {}
                if path.startswith('/git/commits/'): return {'tree': {'sha': 'tree'}}
                raise AssertionError(path)
            with patch.object(api, 'get', side_effect=get), patch.object(api, 'files', return_value=files), \
                 patch.object(ci, 'ledger_at', return_value=set()), patch.object(ci, 'history', return_value=(set(), False)), \
                 patch.object(api, 'entry', return_value={'sha': 'd'*40, 'mode': '100644'}), \
                 patch.object(api, 'blob', return_value=picture()), patch.object(ci, 'compress', return_value=picture()), \
                 patch.object(api, 'write') as write:
                self.assertIsNone(ci.run(api, 1, 'b'*40, False, 'fake'))
                write.assert_not_called()


def signed_commit(sha):
    return {'sha': sha, 'parents': [{'sha': 'a'*40}],
            'author': {'login': 'github-actions[bot]', 'id': 41898282},
            'committer': {'login': 'web-flow', 'id': 19864447},
            'commit': {'message': ci.MARKER, 'tree': {'sha': 'c'*40},
                       'author': {'email': '41898282+github-actions[bot]@users.noreply.github.com'},
                       'committer': {'name': 'GitHub', 'email': 'noreply@github.com'},
                       'verification': {'verified': True, 'reason': 'valid'}}}


class CompletionTests(unittest.TestCase):
    def test_created_unsigned_commit_never_updates_branch(self):
        api = ci.API('Armenm/repo', 'fake')
        unsigned = signed_commit('c'*40)
        unsigned['commit']['verification']['verified'] = False
        def write(path, body, method='POST'):
            return {'sha': ci.git_hash(base64.b64decode(body['content']))} if path == '/git/blobs' else {'sha': 'c'*40}
        with patch.object(api, 'get', side_effect=[pr(), unsigned]), patch.object(api, 'write', side_effect=write) as writes:
            with self.assertRaisesRegex(ci.Failure, 'authenticate'):
                ci.publish(api, 1, pr(), 'main', ('a'*40, 'b'*40), 'tree',
                           [('public/demo/a.png', '100644', picture())], set())
            self.assertFalse(any('/refs/' in c.args[0] for c in writes.call_args_list))

    def test_stdlib_no_candidate_run_and_dry_run(self):
        api = ci.API('Armenm/repo', 'fake')
        p = pr()
        def get(path):
            if path == '': return {'default_branch': 'main'}
            if path == '/branches/main': return {'commit': {'sha': 'b'*40}}
            if path == '/pulls/1': return p
            if path.startswith('/commits/'): return {}
            if path.startswith('/git/commits/'): return {'tree': {'sha': 'tree'}}
            raise AssertionError(path)
        for files, dry in [([], False), ([{'filename': 'public/demo/x.png',
                                           'status': 'added', 'sha': 'd'*40}], True)]:
            p['changed_files'] = len(files)
            with patch.object(api, 'get', side_effect=get), patch.object(api, 'files', return_value=files),                  patch.object(api, 'entry', return_value={'sha': 'd'*40, 'mode': '100644'}),                  patch.object(api, 'blob', return_value=b'data'),                  patch.object(ci, 'image_info', side_effect=AssertionError('Pillow imported')),                  patch.object(ci, 'history', side_effect=AssertionError('history imported')),                  patch.object(ci, 'compress', side_effect=AssertionError('SDK imported')),                  patch.object(api, 'write') as writes:
                self.assertIsNone(ci.run(api, 1, 'b'*40, dry, ''))
                writes.assert_not_called()



class WorkflowTests(unittest.TestCase):
    def test_trusted_boundaries_and_conditional_dependencies(self):
        workflow = Path('.github/workflows/tinify-images.yml').read_text()
        self.assertIn('pull_request_target:', workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('self-hosted, macOS, ARM64', workflow)
        self.assertIn('--dry-run', workflow)
        self.assertIn('steps.preflight.outputs.candidates', workflow)
        self.assertIn('--require-hashes', workflow)
        self.assertIn('TINYPNG_API_KEY', workflow)
        for unsafe in ['pull_request.head.sha', 'pull_request.head.ref', 'pull-requests: write',
                       'checks: write', 'pip install tinify', 'TINIFY_API_KEY']:
            self.assertNotIn(unsafe, workflow)
        import re
        for action in re.findall(r'uses: (.+)', workflow):
            self.assertRegex(action, r'@[0-9a-f]{40}$')

    def test_cli_dispatch_dry_run_and_exact_sha_exit(self):
        event = {'repository': {'default_branch': 'main'}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            event_path, summary = Path(directory)/'event', Path(directory)/'summary'
            event_path.write_text(json.dumps(event))
            env = {'GITHUB_EVENT_PATH': str(event_path), 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'Armenm/repo',
                   'GITHUB_TOKEN': 'fake', 'GITHUB_SHA': 'b'*40,
                   'GITHUB_STEP_SUMMARY': str(summary)}
            with patch.dict('os.environ', env), patch('sys.argv', ['ci.py', '--pr', '1', '--dry-run']),                  patch.object(ci, 'run', return_value=None) as run:
                self.assertEqual(ci.main(), 0)
                self.assertTrue(run.call_args.args[3])
            with patch.dict('os.environ', env), patch('sys.argv', ['ci.py', '--pr', '1']),                  patch.object(ci, 'run', return_value='c'*40):
                self.assertEqual(ci.main(), 2)
                self.assertIn(ci.handoff('c'*40), summary.read_text())
            env['GITHUB_REF'] = 'refs/heads/untrusted'
            with patch.dict('os.environ', env), patch('sys.argv', ['ci.py', '--pr', '1']),                  patch.object(ci, 'run') as run:
                self.assertEqual(ci.main(), 1)
                run.assert_not_called()


class RegressionTests(unittest.TestCase):
    def test_pagination_counts_and_deadline(self):
        api = ci.API('Armenm/repo', 'fake')
        records = [{'filename': str(i)} for i in range(101)]
        with patch.object(api, 'get', side_effect=[records[:100], records[100:]]) as get:
            self.assertEqual(api.files(1, 101), records)
            self.assertEqual(get.call_count, 2)
        with patch.object(api, 'get', return_value=[]):
            with self.assertRaises(ci.Failure):
                api.files(1, 1)
        api.deadline = 0
        with self.assertRaises(ci.Failure):
            api.remaining()

    def test_signed_history_content_and_ledger(self):
        api = ci.API('Armenm/repo', 'fake')
        old, new = picture(compress_level=0), picture()
        values = {hashlib.sha256(x).hexdigest() for x in (old, new)}
        commit = signed_commit('c'*40)
        commit['files'] = [{'filename': 'public/demo/x.png', 'status': 'modified'},
                           {'filename': ci.LEDGER, 'status': 'modified'}]
        def get(path):
            if path.startswith('/compare/'):
                return {'total_commits': 1, 'commits': [commit]}
            if path.startswith('/commits/'): return commit
            return {'tree': {'sha': 'parent'}}
        with patch.object(api, 'get', side_effect=get), \
             patch.object(api, 'entry', side_effect=lambda tree, path: {'sha': tree}), \
             patch.object(api, 'blob', side_effect=lambda sha: old if sha == 'parent' else new), \
             patch.object(ci, 'ledger_at', return_value=values):
            self.assertEqual(ci.history(api, 'b'*40, 'c'*40), (values, True))
            commit['files'][0]['filename'] = 'src/server.ts'
            with self.assertRaisesRegex(ci.Failure, 'non-compression'):
                ci.history(api, 'b'*40, 'c'*40)

    def test_stdlib_only_interpreter_needs_no_key_or_image_packages(self):
        code = """
from tools.tinify_ci import ci
from unittest.mock import patch
import sys
api = ci.API('Armenm/repo', 'fake')
pr = {'state': 'open', 'user': {'login': 'Armenm'}, 'changed_files': 0,
      'head': {'sha': 'a'*40, 'ref': 'feature', 'repo': {'full_name': 'Armenm/repo'}},
      'base': {'sha': 'b'*40, 'ref': 'main', 'repo': {'full_name': 'Armenm/repo'}}}
with patch.object(api, 'get', side_effect=[{'default_branch': 'main'},
        {'commit': {'sha': 'b'*40}}, pr, {}, pr]), patch.object(api, 'files', return_value=[]):
    assert ci.run(api, 1, 'b'*40, False, '') is None
assert 'PIL' not in sys.modules and 'tinify' not in sys.modules
"""
        result = subprocess.run([__import__('sys').executable, '-S', '-c', code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bot_sender_guard(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            event_path = Path(directory)/'event'
            event_path.write_text(json.dumps({'action': 'synchronize', 'sender': {'type': 'Bot'}}))
            with patch.dict('os.environ', {'GITHUB_EVENT_PATH': str(event_path),
                            'GITHUB_EVENT_NAME': 'pull_request_target'}), \
                 patch('sys.argv', ['ci.py', '--pr', '1']), patch.object(ci, 'run') as run:
                self.assertEqual(ci.main(), 0)
                run.assert_not_called()



class LedgerTrustTests(unittest.TestCase):
    def test_pr_ledger_edit_cannot_skip_upload(self):
        api = ci.API('Armenm/repo', 'fake')
        live = pr()
        live['changed_files'] = 2
        raw = picture()
        files = [{'filename': 'public/demo/a.png', 'status': 'modified', 'sha': ci.git_hash(raw)},
                 {'filename': ci.LEDGER, 'status': 'modified', 'sha': 'e'*40}]
        def get(path):
            if path == '': return {'default_branch': 'main'}
            if path == '/branches/main': return {'commit': {'sha': 'b'*40}}
            if path == '/pulls/1': return live
            if path == '/git/commits/' + 'a'*40: return {'tree': {'sha': 'untrusted-head'}}
            if path == '/git/commits/' + 'b'*40: return {'tree': {'sha': 'trusted-base'}}
            raise AssertionError(path)
        with patch.object(api, 'get', side_effect=get), patch.object(api, 'files', return_value=files),              patch.object(ci, 'ledger_at', return_value=set()) as ledger,              patch.object(ci, 'history', return_value=(set(), False)),              patch.object(api, 'entry', return_value={'sha': ci.git_hash(raw), 'mode': '100644'}) as entry,              patch.object(api, 'blob', return_value=raw), patch.object(ci, 'compress', return_value=raw) as upload,              patch.object(api, 'write') as writes:
            self.assertIsNone(ci.run(api, 1, 'b'*40, False, 'fake'))
            ledger.assert_called_once_with(api, 'trusted-base')
            entry.assert_called_once_with('untrusted-head', 'public/demo/a.png')
            upload.assert_called_once()
            writes.assert_not_called()


class ReviewRegressionTests(unittest.TestCase):
    def test_sdk_transport_caps_real_response_before_buffering(self):
        import requests
        import tinify
        raw = picture(compress_level=0)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source, output = Path(directory) / 'in', Path(directory) / 'out'
            source.write_bytes(raw)
            streams = []
            class CountedStream(io.BytesIO):
                read_bytes = 0
                def read(self, size=-1):
                    data = super().read(size)
                    self.read_bytes += len(data)
                    return data
            def send(adapter, request, **kwargs):
                response = requests.Response()
                response.status_code = 200
                response.headers['location'] = 'https://api.tinify.com/output/test'
                response.raw = CountedStream(b'x' * 2048)
                streams.append(response.raw)
                return response
            with patch.object(ci, 'MAX_IMAGE', 1024), \
                 patch('requests.adapters.HTTPAdapter.send', new=send), \
                 patch.dict('os.environ', {'TINYPNG_API_KEY': 'fake'}):
                with self.assertRaises(tinify.Error):
                    ci.worker(source, output)
            self.assertFalse(output.exists())
            self.assertTrue(all(stream.closed and stream.read_bytes <= 1025 for stream in streams))

    def test_publication_attempt_retains_exact_sha_notice_on_readback_failure(self):
        api = ci.API('Armenm/repo', 'fake')
        output = 'c' * 40
        def write(path, body, method='POST'):
            if path == '/git/blobs':
                return {'sha': ci.git_hash(base64.b64decode(body['content']))}
            if path.startswith('/git/refs/'):
                return {'object': {'sha': output}}
            return {'sha': output}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            summary = Path(directory) / 'summary'
            with patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': str(summary)}), \
                 patch.object(api, 'get', side_effect=[pr(), signed_commit(output), pr(), ci.Failure('API')]), \
                 patch.object(api, 'write', side_effect=write):
                with self.assertRaises(ci.Failure):
                    ci.publish(api, 1, pr(), 'main', ('a'*40, 'b'*40), 'tree',
                               [('public/demo/a.png', '100644', picture())], set())
            self.assertTrue(summary.exists())
            text = summary.read_text()
            self.assertIn(output, text)
            self.assertIn('uncertain', text)
            self.assertIn('ACTION REQUIRED', text)
            self.assertIn('no run is queued', text)

    def test_no_candidate_bot_head_still_requires_approval_without_image_packages(self):
        api = ci.API('Armenm/repo', 'fake')
        live = pr()
        live['changed_files'] = 1
        bot = signed_commit('a' * 40)
        bot['files'] = [{'filename': ci.LEDGER, 'status': 'modified'},
                        {'filename': 'public/demo/a.png', 'status': 'modified'}]
        def get(path):
            if path == '': return {'default_branch': 'main'}
            if path == '/branches/main': return {'commit': {'sha': 'b' * 40}}
            if path == '/pulls/1': return live
            if path == '/commits/' + 'a' * 40 + '?per_page=100': return bot
            raise AssertionError(path)
        with patch.object(api, 'get', side_effect=get), \
             patch.object(api, 'files', return_value=[{'filename': ci.LEDGER, 'status': 'modified'}]), \
             patch.object(ci, 'image_info', side_effect=AssertionError('Pillow unavailable')), \
             patch.object(ci, 'compress', side_effect=AssertionError('SDK unavailable')), \
             patch.object(api, 'write') as writes:
            # Dry-run is the workflow's stdlib preflight. A returned SHA makes it fail
            # with the exact-SHA handoff even when later dependency/optimizer steps skip.
            self.assertEqual(ci.run(api, 1, 'b' * 40, True, ''), 'a' * 40)
            writes.assert_not_called()

    def test_bot_head_rerun_repeats_exact_sha_handoff(self):
        api = ci.API('Armenm/repo', 'fake')
        live = pr()
        def get(path):
            if path == '': return {'default_branch': 'main'}
            if path == '/branches/main': return {'commit': {'sha': 'b'*40}}
            if path == '/pulls/1': return live
            return {'tree': {'sha': 'tree'}}
        with patch.object(api, 'get', side_effect=get), \
             patch.object(api, 'files', return_value=[{'filename': 'public/demo/a.png', 'status': 'modified'}]), \
             patch.object(ci, 'ledger_at', return_value=set()), \
             patch.object(ci, 'history', return_value=(set(), True)), patch.object(api, 'write') as writes:
            self.assertEqual(ci.run(api, 1, 'b'*40, False, ''), 'a'*40)
            writes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
