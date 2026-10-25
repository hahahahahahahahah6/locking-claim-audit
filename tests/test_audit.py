import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from locking_claim_audit.audit import LOCKED, NOLOCK, audit, extract_claims, extract_evidence, main, normalize


class ClaimTests(unittest.TestCase):
    def families(self, text):
        return {c['family'] for c in extract_claims(text)}

    def test_normalization(self):
        self.assertEqual(normalize("DOESN'T   file-lock"), 'does not file lock')

    def test_overlap(self):
        self.assertEqual(self.families('does not use a lock'), {'nolock'})

    def test_contraction(self):
        self.assertEqual(self.families("doesn't lock"), {'nolock'})

    def test_hyphen_file(self):
        self.assertEqual(self.families('file-locking'), {'locked'})

    def test_hyphen_concurrent(self):
        self.assertEqual(self.families('concurrent-safe'), {'locked'})

    def test_never(self):
        self.assertFalse(self.families('never locked'))

    def test_bare_lock(self):
        self.assertFalse(self.families('a lock'))

    def test_bare_lock_file(self):
        self.assertFalse(self.families('a lock file'))

    def test_negated_unlisted(self):
        self.assertFalse(self.families('does not claim to use locking'))

    def test_negation_sentence(self):
        self.assertFalse(self.families('Never promises file locking.'))

    def test_inline(self):
        self.assertEqual(self.families('`file locking`'), {'locked'})

    def test_fences(self):
        self.assertFalse(self.families('```text\nfile locking\n```\n~~~\nno locking\n~~~'))

    def test_long_fence(self):
        self.assertFalse(self.families('````\n```\nfile locking\n````'))

    def test_wrapped_sentence(self):
        self.assertEqual(self.families('safe for\nconcurrent runs'), {'locked'})

    def test_word_boundary(self):
        self.assertFalse(self.families('file lockings'))

    def test_optional_middle_not_allowed(self):
        self.assertFalse(self.families('uses some locking'))

    def test_independent_sentences(self):
        self.assertEqual(self.families('No locking. File locking.'), {'locked', 'nolock'})


class EvidenceTests(unittest.TestCase):
    def evidence(self, source):
        return extract_evidence(source, 'main.py')

    def test_alias(self):
        self.assertTrue(self.evidence('import fcntl as fc\nfc.flock(1, 2)'))

    def test_from_alias(self):
        self.assertTrue(self.evidence('from fcntl import flock as take\ntake(1, 2)'))

    def test_bare_imported_module(self):
        self.assertTrue(self.evidence('import fcntl\nflock(1, 2)'))

    def test_bare_unimported(self):
        self.assertFalse(self.evidence('flock(1, 2)'))

    def test_locking_bare_unimported(self):
        self.assertFalse(self.evidence('locking(1, 2)'))

    def test_dedupe(self):
        self.assertEqual(len(self.evidence('fcntl.flock(1, 2); fcntl.flock(3, 4)')), 1)

    def test_dead_code(self):
        self.assertTrue(self.evidence('if False:\n fcntl.flock(1, 2)'))

    def test_flags_variable(self):
        self.assertFalse(self.evidence('flags = os.O_CREAT | os.O_EXCL\nos.open("data", flags)'))

    def test_two_kinds(self):
        self.assertEqual(len(self.evidence('os.open("data.lock", os.O_CREAT | os.O_EXCL)')), 2)


POSITIVE = [
    'fcntl.flock(1, 2)', 'fcntl.lockf(1, 2)', 'msvcrt.locking(1, 2, 3)',
    'import filelock', 'from filelock import FileLock', 'import portalocker',
    'import fasteners', 'import flufl.lock', 'from flufl import lock',
    'FileLock("x")', 'SoftFileLock("x")', 'UnixFileLock("x")', 'WindowsFileLock("x")',
    'portalocker.Lock("x")', 'fasteners.InterProcessLock("x")', 'flufl.lock.Lock("x")',
    'os.open("x", os.O_CREAT | os.O_EXCL)', 'os.open("x", O_CREAT | O_EXCL)',
    'open("data.lock", "w")', 'io.open("data.lock")', 'os.open("data.lock", 1)',
    'Path.open("data.lock")', 'Path.write_text("data.lock", "x")',
    'Path.write_bytes("data.lock", b"x")', 'Path("x").open("data.lock")',
    'from pathlib import Path as P\nP.open("data.lock")',
    'from pathlib import Path as P\nP("data").open("data.lock")',
    'from pathlib import Path\np = Path("data")\np.open("data.lock")',
    'import pathlib\np: pathlib.Path = pathlib.Path("data")\np.write_text("data.lock")',
    'os.mkdir("data.lock")', 'os.makedirs("data.lock")',
]
NEGATIVE = [
    'Lock()', 'threading.Lock()', 'multiprocessing.Lock()', 'asyncio.Lock()',
    'from threading import Lock\nLock()', 'lock', '"locking"', '# fcntl.flock(1, 2)',
    'LockTimeout()', 'my_lock.acquire()', 'my_lock.release()',
    'subprocess.run("flock data command", shell=True)', 'os.open("data", os.O_CREAT)',
    'open("data", "w")', 'os.mkdir("data")',
]


def evidence_case(source, expected):
    def test(self):
        self.assertEqual(bool(self.evidence(source)), expected, source)
    return test


for index, source in enumerate(POSITIVE):
    setattr(EvidenceTests, 'test_positive_%02d' % index, evidence_case(source, True))
for index, source in enumerate(NEGATIVE):
    setattr(EvidenceTests, 'test_negative_%02d' % index, evidence_case(source, False))
for index, phrase in enumerate(LOCKED + NOLOCK):
    def phrase_case(self, phrase=phrase):
        expected = 'locked' if phrase in LOCKED else 'nolock'
        self.assertEqual(self.families(phrase), {expected})
    setattr(ClaimTests, 'test_phrase_%02d' % index, phrase_case)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def result(self, doc='file locking', source='pass'):
        self.write('README.md', doc)
        self.write('main.py', source)
        return audit(self.root)

    def test_locked_false(self):
        self.assertEqual(self.result()['findings'][0]['rule'], 'documented-locked-claim-false')

    def test_locked_verified(self):
        self.assertEqual(self.result(source='fcntl.flock(1, 2)')['verified_claims'][0]['family'], 'locked')

    def test_nolock_false(self):
        self.assertEqual(self.result('no locking', 'fcntl.flock(1, 2)')['findings'][0]['rule'], 'documented-nolock-claim-false')

    def test_nolock_verified(self):
        self.assertEqual(self.result('no locking')['verified_claims'][0]['family'], 'nolock')

    def test_no_claim(self):
        self.assertFalse(self.result('Hello', 'fcntl.flock(1, 2)')['findings'])

    def test_both_no_source(self):
        self.write('README.md', 'File locking. No locking.')
        result = audit(self.root)
        self.assertEqual(len(result['findings']), 2)
        self.assertTrue(all(f['evidence'] == [{'kind': 'no-source'}] for f in result['findings']))

    def test_testonly_locked(self):
        self.write('tests/test_work.py', 'fcntl.flock(1, 2)')
        finding = self.result()['findings'][0]
        self.assertEqual(finding['severity'], 'MEDIUM')
        self.assertEqual(finding['rule'], 'documented-locked-claim-false-test-only')

    def test_testonly_nolock(self):
        self.write('fixtures/main.py', 'fcntl.flock(1, 2)')
        self.assertEqual(self.result('no locking')['findings'][0]['rule'], 'documented-nolock-claim-false-test-only')

    def test_test_filename(self):
        self.write('work_test.py', 'fcntl.flock(1, 2)')
        self.assertEqual(self.result()['findings'][0]['severity'], 'MEDIUM')

    def test_ignore(self):
        self.result(source='fcntl.flock(1, 2)')
        self.write('other.py', 'pass')
        self.assertEqual(audit(self.root, ['main.*'])['findings'][0]['severity'], 'HIGH')

    def test_docs_nested(self):
        self.write('docs/nested/guide.md', 'file locking')
        self.write('main.py', 'pass')
        self.assertEqual(len(audit(self.root)['findings']), 1)

    def test_other_markdown_ignored(self):
        self.write('notes.md', 'file locking')
        self.assertFalse(audit(self.root)['claims'])

    def test_examples_docs_source_excluded(self):
        self.write('examples/main.py', 'fcntl.flock(1, 2)')
        self.write('docs/main.py', 'fcntl.flock(1, 2)')
        self.assertFalse(self.result()['production_evidence'])

    def test_symlink(self):
        self.write('hidden/source.txt', 'fcntl.flock(1, 2)')
        (self.root / 'linked.py').symlink_to(self.root / 'hidden/source.txt')
        self.assertFalse(self.result()['production_evidence'])

    def test_symlink_directory(self):
        with tempfile.TemporaryDirectory() as other:
            Path(other, 'main.py').write_text('fcntl.flock(1, 2)')
            (self.root / 'external').symlink_to(other, target_is_directory=True)
            self.assertFalse(self.result()['production_evidence'])

    def test_env_never_read(self):
        self.write('.env', 'ENV_MARKER_DO_NOT_READ')
        self.write('.env.py', 'ENV_MARKER_DO_NOT_READ')
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path.name.startswith('.env'):
                raise AssertionError('environment contents opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            result = self.result()
        self.assertNotIn('ENV_MARKER_DO_NOT_READ', json.dumps(result))

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([str(self.root)] + list(args))
        return code, output.getvalue()

    def test_json(self):
        self.result()
        code, output = self.run_cli('--json')
        self.assertEqual(code, 2)
        self.assertEqual(set(json.loads(output)) & {'findings', 'verified_claims'}, {'findings', 'verified_claims'})

    def test_fail_threshold(self):
        self.write('tests/test_work.py', 'fcntl.flock(1, 2)')
        self.result()
        code, output = self.run_cli('--fail-on', 'HIGH')
        self.assertEqual(code, 0)
        self.assertIn('MEDIUM', output)

    def test_default_medium_fails(self):
        self.write('tests/test_work.py', 'fcntl.flock(1, 2)')
        self.result()
        self.assertEqual(self.run_cli()[0], 2)

    def test_version(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(['--version'])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), '0.1.0')

    def test_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--unknown'])
        self.assertEqual(caught.exception.code, 1)

    def test_parse_error(self):
        self.result(source='def broken(')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.run_cli()
        self.assertEqual(caught.exception.code, 1)

    def test_verbose(self):
        self.result('Hello')
        self.assertIn('production_evidence', self.run_cli('--verbose')[1])


if __name__ == '__main__':
    unittest.main()
