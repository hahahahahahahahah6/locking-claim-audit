"""Install both distributions in fresh environments and assert CLI contracts.

Run after building: python3 tests/smoke.py
"""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd, expected=0):
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    result = subprocess.run(command, cwd=cwd, env=environment, text=True, capture_output=True)
    if result.returncode != expected:
        raise AssertionError((command, result.returncode, result.stdout, result.stderr))
    return result.stdout


def main():
    wheel, = (ROOT / 'dist').glob('*.whl')
    sdist, = (ROOT / 'dist').glob('*.tar.gz')
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        for fixture in ('good/locked', 'good/nolock', 'bad/locked', 'bad/nolock', 'testonly'):
            assert any(n.endswith('tests/fixtures/' + fixture + '/README.md') for n in names)
        assert any(n.endswith('tests/fixtures/testonly/tests/test_work.py') for n in names)
    for artifact in (wheel, sdist):
        with tempfile.TemporaryDirectory(prefix='locking-audit-smoke-') as directory:
            temporary = Path(directory)
            environment = temporary / 'venv'
            venv.EnvBuilder(with_pip=True).create(environment)
            interpreter = str(environment / 'bin/python3')
            console = str(environment / 'bin/locking-claim-audit')
            run([interpreter, '-m', 'pip', 'install', '--no-deps', str(artifact)], temporary)
            for entry in ([console], [interpreter, '-m', 'locking_claim_audit']):
                assert run(entry + ['--version'], temporary).strip() == '0.1.0'
                for fixture, family, expected, rule in (
                    ('good/locked', 'locked', 0, None),
                    ('good/nolock', 'nolock', 0, None),
                    ('bad/locked', 'locked', 2, 'documented-locked-claim-false'),
                    ('bad/nolock', 'nolock', 2, 'documented-nolock-claim-false'),
                    ('testonly', 'locked', 2, 'documented-locked-claim-false-test-only'),
                ):
                    target = str(ROOT / 'tests/fixtures' / fixture)
                    report = json.loads(run(entry + [target, '--json'], temporary, expected))
                    if rule:
                        assert len(report['findings']) == 1, report
                        finding = report['findings'][0]
                        assert finding['rule'] == rule, finding
                        assert finding['severity'] == ('MEDIUM' if fixture == 'testonly' else 'HIGH')
                    else:
                        assert report['findings'] == [], report
                        assert [v['family'] for v in report['verified_claims']] == [family], report
                target = str(ROOT / 'tests/fixtures/testonly')
                report = json.loads(run(entry + [target, '--json', '--fail-on', 'HIGH'], temporary))
                assert len(report['findings']) == 1 and report['findings'][0]['severity'] == 'MEDIUM'
                report = json.loads(run(entry + [str(ROOT), '--json'], temporary))
                assert report['claims'] == [] and report['findings'] == [], report
                output = run(entry + [str(ROOT / 'tests/fixtures/good/nolock')], temporary)
                assert 'PASS' in output and 'FIXTURE_ENV_MARKER_ONLY' not in output
            print(artifact.name + ': fresh install, both entry points, version, JSON, five fixtures, HIGH threshold, self-audit: PASS')


if __name__ == '__main__':
    main()
