"""Offline documentation and cross-process exclusion evidence analysis."""
import argparse
import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

LOCKED = (
    'file locking', 'uses locking', 'uses a lock', 'uses locks',
    'locking support', 'supports locking', 'safe for concurrent runs',
    'safe to run concurrently', 'concurrent safe', 'prevents concurrent runs',
    'prevents concurrent access', 'serializes concurrent access',
    'serialises concurrent access', 'uses a lock file', 'creates a lock file',
    'protected by a lock', 'guarded by a lock', 'acquires a lock',
)
NOLOCK = (
    'no locking', 'does not lock', 'does not use locking',
    'does not use a lock', 'without locking', 'no lock file', 'no lock',
    'not safe for concurrent runs', 'not safe to run concurrently',
    'do not run concurrently', 'must not run concurrently',
)
LIBRARIES = ('filelock', 'portalocker', 'fasteners', 'flufl.lock')


def normalize(text):
    return ' '.join(text.lower().replace("n't", ' not').replace('-', ' ').split())


def phrase_pattern(phrase):
    return r'(?<!\w)' + re.escape(phrase) + r'(?!\w)'


def extract_claims(text, path='README.md'):
    """Return sentence-level claims, ignoring fenced Markdown blocks."""
    lines = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
            lines.append((number, ''))
        else:
            lines.append((number, line if fence is None else ''))
    # Paragraphs keep wrapped sentences together; offsets retain source locations.
    joined = '\n'.join(line for _, line in lines)
    claims = []
    for match in re.finditer(r'[^.!?\n]+(?:\n(?!\s*\n)[^.!?\n]+)*[.!?]?', joined):
        sentence = match.group().strip()
        if not sentence:
            continue
        normalized = normalize(sentence)
        masked = normalized
        negative = False
        for phrase in sorted(NOLOCK, key=len, reverse=True):
            pattern = phrase_pattern(phrase)
            if re.search(pattern, masked):
                negative = True
                masked = re.sub(pattern, lambda m: ' ' * len(m.group()), masked)
        families = ['nolock'] if negative else []
        if not re.search(r'\b(no|not|never|without)\b', normalized):
            if any(re.search(phrase_pattern(p), masked) for p in LOCKED):
                families.append('locked')
        for family in families:
            claims.append({'family': family, 'path': path,
                           'line': joined.count('\n', 0, match.start()) + 1,
                           'sentence': sentence})
    return claims


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + '.' + node.attr if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return ''


def extract_evidence(source, path):
    tree = ast.parse(source, filename=path)
    aliases = {}
    imported = set()
    evidence = {}

    def add(node, kind):
        evidence[(node.lineno, kind)] = {'path': path, 'line': node.lineno, 'kind': kind}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                imported.add(item.name)
                if any(item.name == lib or item.name.startswith(lib + '.') for lib in LIBRARIES):
                    add(node, 'library-import')
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            imported.add(module)
            for item in node.names:
                aliases[item.asname or item.name] = module + '.' + item.name
            if any(module == lib or module.startswith(lib + '.') or
                   module + '.' + item.name == lib
                   for lib in LIBRARIES for item in node.names):
                add(node, 'library-import')

    path_objects = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            constructor = dotted(node.value.func)
            first, dot, rest = constructor.partition('.')
            resolved = aliases.get(first, first) + (dot + rest if dot else '')
            if resolved in ('Path', 'pathlib.Path'):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                path_objects.update(target.id for target in targets if isinstance(target, ast.Name))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw = dotted(node.func)
        first, dot, rest = raw.partition('.')
        name = aliases.get(first, first) + (dot + rest if dot else '')
        if raw in ('flock', 'lockf') and 'fcntl' in imported:
            name = 'fcntl.' + raw
        if raw == 'locking' and 'msvcrt' in imported:
            name = 'msvcrt.locking'
        if name in ('fcntl.flock', 'fcntl.lockf', 'msvcrt.locking'):
            add(node, 'os-lock-call')
        if name.endswith('FileLock') or name in ('portalocker.Lock', 'fasteners.InterProcessLock', 'flufl.lock.Lock'):
            add(node, 'lock-constructor')
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        nodes = [child for arg in arguments for child in ast.walk(arg)]
        flags = {child.attr if isinstance(child, ast.Attribute) else child.id
                 for child in nodes if isinstance(child, (ast.Attribute, ast.Name))}
        if name == 'os.open' and {'O_CREAT', 'O_EXCL'} <= flags:
            add(node, 'exclusive-create')
        lock_path = any(isinstance(child, ast.Constant) and isinstance(child.value, str)
                        and '.lock' in child.value for child in nodes)
        path_method = (raw.endswith(('.open', '.write_text', '.write_bytes')) and
                       (name.startswith('Path.') or name.startswith('pathlib.Path.') or
                        first in path_objects or
                        isinstance(getattr(node.func, 'value', None), ast.Call) and
                        (aliases.get(dotted(node.func.value.func), dotted(node.func.value.func)) in
                         ('Path', 'pathlib.Path'))))
        if lock_path and (name in ('open', 'io.open', 'os.open') or path_method):
            add(node, 'lock-file-path')
        if lock_path and name in ('os.mkdir', 'os.makedirs'):
            add(node, 'lock-directory-path')
    return sorted(evidence.values(), key=lambda e: (e['path'], e['line'], e['kind']))


def audit(root, ignore=()):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('audit root must be a directory')
    claims, production, test_only, errors = [], [], [], []
    source_count = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        rel = relative.as_posix()
        if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in ignore):
            continue
        if not path.is_file() or any(p == '.env' or p.startswith('.env.') for p in relative.parts):
            continue
        is_doc = rel == 'README.md' or (relative.parts[0] == 'docs' and path.suffix == '.md')
        if is_doc:
            claims.extend(extract_claims(path.read_text(encoding='utf-8'), rel))
        if path.suffix != '.py' or any(p in ('docs', 'examples') for p in relative.parts[:-1]):
            continue
        testing = any(p in ('tests', 'fixtures') for p in relative.parts[:-1]) or path.name.startswith('test_') or path.name.endswith('_test.py')
        if not testing:
            source_count += 1
        try:
            items = extract_evidence(path.read_bytes(), rel)
        except (SyntaxError, ValueError) as exc:
            errors.append({'path': rel, 'error': str(exc)})
            continue
        (test_only if testing else production).extend(items)
    findings, verified = [], []
    for family in ('locked', 'nolock'):
        selected = [c for c in claims if c['family'] == family]
        if not selected:
            continue
        severity, suffix, evidence = None, '', []
        if source_count == 0:
            severity, evidence = 'HIGH', [{'kind': 'no-source'}]
        elif family == 'locked' and not production:
            severity = 'MEDIUM' if test_only else 'HIGH'
            suffix = '-test-only' if test_only else ''
            evidence = test_only
        elif family == 'nolock' and production:
            severity, evidence = 'HIGH', production
        elif family == 'nolock' and test_only:
            severity, suffix, evidence = 'MEDIUM', '-test-only', test_only
        if severity:
            findings.append({'rule': 'documented-' + family + '-claim-false' + suffix,
                             'severity': severity, 'family': family,
                             'claims': selected, 'evidence': evidence})
        else:
            verified.append({'family': family, 'claims': selected, 'evidence': production if family == 'locked' else []})
    return {'findings': findings, 'verified_claims': verified, 'claims': claims,
            'production_evidence': production, 'test_only_evidence': test_only,
            'source_errors': errors}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, self.prog + ': error: ' + message + '\n')


def main(argv=None):
    parser = Parser(prog='locking-claim-audit', description='Offline audit of documented cross-process exclusion promises against Python source.')
    parser.add_argument('path', nargs='?', default='.')
    parser.add_argument('--json', action='store_true', dest='as_json')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--ignore', action='append', default=[], metavar='GLOB')
    parser.add_argument('--fail-on', choices=('MEDIUM', 'HIGH'), default='MEDIUM')
    parser.add_argument('--version', action='version', version='0.1.0')
    args = parser.parse_args(argv)
    try:
        result = audit(args.path, args.ignore)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if result['source_errors']:
        parser.error('could not parse source: ' + json.dumps(result['source_errors']))
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        for finding in result['findings']:
            print(finding['severity'] + ' ' + finding['rule'])
            for claim in finding['claims']:
                print('  {path}:{line}: {sentence}'.format(**claim))
            for item in finding['evidence']:
                print('  ' + json.dumps(item, sort_keys=True))
        if not result['findings']:
            print('PASS')
        if args.verbose:
            print(json.dumps(result, indent=2))
    return 2 if any(f['severity'] == 'HIGH' or args.fail_on == 'MEDIUM' for f in result['findings']) else 0
