"""Offline paired ASR scoring, never an ASR runner or transcript corrector.

Run: python tools/glossary_asr_eval.py --manifest /private/pair.json
     --output /private/report.json

Manifest v1 has normalization="nfkc-v1", dataset_kind="synthetic"|"human",
audio={path,sha256}, glossary={path,sha256,source_version}, and cases=[...].
Each case has truth_ref={path,sha256,human_reviewed:true}, annotations=[
{category,start,end}], and without_glossary/with_glossary output records.
Offsets address the ORIGINAL reference's Unicode code points, before NFKC.
Output records have path,sha256,audio_sha256,delivery_verified:true,
model_error:false,representation="provider_original",provider,model,
glossary_applied:bool; the enabled record also binds glossary_sha256 and
glossary_source_version. All paths resolve relative to the private manifest.

Categories are defined below. Disambiguated homophones additionally require
context_reviewed:true. Ordinary negatives can specify forbidden_replacements
for exact aligned-span false-substitution counts. Ambiguous homophones are
never scored against a canonical spelling; all lexical alignment/CER is unknown
for that case (use separate cases for independently scoreable material).
Silence uses an empty reference and one silence annotation at [0,0).

All human review, delivery and provider-original flags are operator attestations,
not independently proven by this tool. It verifies bytes/identity, not audible
delivery or model consumption. CER/span alignment is lexical, NOT a whole-sentence
semantic verdict or proof glossary caused an improvement. Original files remain
unchanged/private; reports contain only hashes, fixed labels and numeric scores.
There is no extra case folding, punctuation/whitespace stripping, Traditional/
Simplified conversion or homophone replacement after NFKC's compatibility maps;
provider files are never rewritten. Alignment tie order is
diagonal, deletion, insertion; ambiguous edit alignments are not linguistic proof.
Exit zero means scoring completed, NOT accuracy, semantic or glossary-effect
qualification. Exit two means at least some evidence is unknown/ambiguous.
"""
import argparse
from array import array
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata


NORMALIZATION = 'nfkc-v1'
CATEGORIES = ('term', 'abbreviation', 'mixed_language', 'person',
    'homophone_disambiguated', 'homophone_ambiguous', 'ordinary_negative',
    'self_correction', 'silence')
OUTPUTS = ('without_glossary', 'with_glossary')
OUTCOMES = ('hit', 'miss', 'wrong', 'ambiguous', 'unknown')
MAX_TEXT_BYTES = 262144
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_CELLS = 4_000_000
REPOSITORY = Path(__file__).resolve().parents[1]


class ContractError(ValueError):
    """Only fixed codes may cross the private-file/report boundary."""


def _require(condition, code):
    if not condition:
        raise ContractError(code)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _identity(value):
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError:
            return False
        return 0 < len(value) <= 256
    return type(value) is int and value >= 0


@contextmanager
def _private_stream(path, limit):
    """Validate and read the same non-following FD; never open a FIFO blocking."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK'):
        raise ContractError('secure_file_open_unsupported')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode), 'file_not_regular')
        _require(info.st_mode & 0o077 == 0 and info.st_uid == os.getuid(), 'file_not_private')
        _require(info.st_size <= limit, 'file_size_limit')
        yield stream


def _private_bytes(path, limit):
    with _private_stream(path, limit) as stream:
        raw = stream.read(limit + 1)
        _require(len(raw) <= limit, 'file_size_limit')
        return raw


def _file(record, base_dir, *, limit=MAX_TEXT_BYTES, text=True):
    _require(isinstance(record, dict) and _hash(record.get('sha256'))
        and isinstance(record.get('path'), str) and record['path'], 'file_identity_missing')
    path = Path(record['path'])
    if not path.is_absolute():
        path = base_dir / path
    try:
        raw = bytearray()
        digest = hashlib.sha256()
        with _private_stream(path, limit) as stream:
            size = 0
            while chunk := stream.read(65536):
                size += len(chunk)
                _require(size <= limit, 'file_size_limit')
                digest.update(chunk)
                if text:
                    raw.extend(chunk)
        _require(digest.hexdigest() == record['sha256'], 'file_hash_mismatch')
        return raw.decode('utf-8') if text else None
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('file_unavailable') from None


def _normalize(text):
    return unicodedata.normalize('NFKC', text)


def _alignment_ambiguity(reference, hypothesis, rows):
    """Conservative per-reference ambiguity over every optimal edit path.

    Reuse the bounded distance matrix; two reachability rows avoid enumerating
    paths. Different mapped positions/deletion or any insertion at a reference
    position make that position uncertain. Even equal repeated characters can
    therefore be unknown; this is attribution evidence, not a semantic guess.
    """
    n, m = len(reference), len(hypothesis)
    assigned, ambiguous = array('i', [-2]) * n, bytearray(n)
    reachable = bytearray(m + 1)
    reachable[m] = 1
    for i in range(n, -1, -1):
        previous = bytearray(m + 1)
        for j in range(m, -1, -1):
            if not reachable[j]:
                continue
            value = rows[i][j]
            if i and j and value == rows[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]):
                previous[j - 1] = 1
                if assigned[i - 1] not in (-2, j - 1):
                    ambiguous[i - 1] = 1
                assigned[i - 1] = j - 1
            if i and value == rows[i - 1][j] + 1:
                previous[j] = 1
                if assigned[i - 1] not in (-2, -1):
                    ambiguous[i - 1] = 1
                assigned[i - 1] = -1
            if j and value == rows[i][j - 1] + 1:
                reachable[j - 1] = 1
                if n:
                    ambiguous[max(0, i - 1)] = 1
        reachable = previous
    return ambiguous


def _align(reference, hypothesis, *, identify_ambiguity=False):
    """Unit-cost edit distance and one deterministic reference-span alignment."""
    n, m = len(reference), len(hypothesis)
    _require((n + 1) * (m + 1) <= MAX_CELLS, 'alignment_resource_limit')
    rows = [array('I', range(m + 1))]
    for i, expected in enumerate(reference, 1):
        previous = rows[-1]
        row = array('I', [i])
        for j, actual in enumerate(hypothesis, 1):
            row.append(min(previous[j - 1] + (expected != actual),
                previous[j] + 1, row[j - 1] + 1))
        rows.append(row)
    i, j = n, m
    mapped = []
    while i or j:
        if i and j and rows[i][j] == rows[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]):
            mapped.append((i - 1, hypothesis[j - 1])); i -= 1; j -= 1
        elif i and rows[i][j] == rows[i - 1][j] + 1:
            i -= 1
        else:
            mapped.append((max(0, i - 1), hypothesis[j - 1])); j -= 1
    mapped.reverse()
    ambiguous = _alignment_ambiguity(reference, hypothesis, rows) if identify_ambiguity else bytearray()
    return rows[n][m], mapped, ambiguous


def _annotations(case, reference):
    annotations = case.get('annotations')
    _require(isinstance(annotations, list) and 0 < len(annotations) <= 256,
        'annotations_missing')
    used = set()
    for item in annotations:
        _require(isinstance(item, dict) and item.get('category') in CATEGORIES,
            'annotation_invalid')
        start, end = item.get('start'), item.get('end')
        _require(type(start) is int and type(end) is int and 0 <= start <= end <= len(reference),
            'annotation_invalid')
        if item['category'] == 'silence':
            _require(reference == '' and start == end == 0 and len(annotations) == 1,
                'silence_reference_invalid')
        else:
            _require(start < end, 'annotation_invalid')
            _require(_normalize(reference[:start]) + _normalize(reference[start:end])
                + _normalize(reference[end:]) == _normalize(reference),
                'annotation_normalization_boundary')
        _require(not used.intersection(range(start, end)), 'annotation_overlap')
        used.update(range(start, end))
        forbidden = item.get('forbidden_replacements', [])
        _require(isinstance(forbidden, list) and len(forbidden) <= 32
            and all(isinstance(word, str) and 0 < len(word) <= 256 for word in forbidden),
            'annotation_invalid')
    return annotations


def _output(record, name, audio_sha, glossary, base_dir):
    _require(isinstance(record, dict), 'output_missing')
    _require(record.get('delivery_verified') is True, 'audio_delivery_unverified')
    _require(record.get('model_error') is False, 'model_result_unknown')
    _require(record.get('representation') == 'provider_original', 'provider_original_unverified')
    _require(record.get('audio_sha256') == audio_sha, 'audio_identity_mismatch')
    _require(all(isinstance(record.get(key), str) and 0 < len(record[key]) <= 256
        for key in ('provider', 'model')), 'provider_identity_missing')
    _require(record.get('glossary_applied') is (name == 'with_glossary'), 'glossary_condition_invalid')
    if name == 'with_glossary':
        _require(record.get('glossary_sha256') == glossary['sha256']
            and type(record.get('glossary_source_version')) is type(glossary['source_version'])
            and record.get('glossary_source_version') == glossary['source_version'],
            'glossary_identity_mismatch')
    return _file(record, base_dir)


def _score(reference, hypothesis, annotations):
    normalized, actual = _normalize(reference), _normalize(hypothesis)
    counts = dict.fromkeys(OUTCOMES, 0)
    categories = {}
    ambiguous = any(item['category'] == 'homophone_ambiguous' for item in annotations)
    context_unknown = any(item['category'] == 'homophone_disambiguated'
        and item.get('context_reviewed') is not True for item in annotations)
    if ambiguous or context_unknown:
        for item in annotations:
            outcome = 'ambiguous' if item['category'] == 'homophone_ambiguous' else 'unknown'
            counts[outcome] += 1
            per = categories.setdefault(item['category'], {**dict.fromkeys(OUTCOMES, 0),
                'ordinary_false_substitutions': 0, 'ordinary_false_substitution_unknown': 0})
            per[outcome] += 1
        return {'status': 'ambiguous' if ambiguous else 'unknown', 'counts': counts,
            'categories': categories, 'cer': None, 'raw_retained_unmodified': True}
    errors, mapped, uncertain = _align(normalized, actual,
        identify_ambiguity=any(item['category'] == 'ordinary_negative' for item in annotations))
    for item in annotations:
        category = item['category']
        per = categories.setdefault(category, {**dict.fromkeys(OUTCOMES, 0),
            'ordinary_false_substitutions': 0, 'ordinary_false_substitution_unknown': 0})
        if category == 'homophone_ambiguous':
            outcome = 'ambiguous'
        elif category == 'homophone_disambiguated' and item.get('context_reviewed') is not True:
            outcome = 'unknown'
        elif category == 'silence':
            outcome = 'hit' if actual == '' else 'wrong'
        else:
            start = len(_normalize(reference[:item['start']]))
            end = len(_normalize(reference[:item['end']]))
            expected = normalized[start:end]
            observed = ''.join(char for at, char in mapped if start <= at < end)
            outcome = 'hit' if observed == expected else 'miss' if not observed else 'wrong'
            if category == 'ordinary_negative':
                forbidden = [_normalize(word) for word in item.get('forbidden_replacements', [])]
                prefix, suffix = normalized[:start], normalized[end:]
                anchored = (actual.startswith(prefix) and actual.endswith(suffix)
                    and len(actual) >= len(prefix) + len(suffix))
                if anchored:
                    observed = actual[len(prefix):len(actual)-len(suffix) if suffix else len(actual)]
                    outcome = 'hit' if observed == expected else 'miss' if not observed else 'wrong'
                elif observed not in forbidden or any(uncertain[start:end]):
                    outcome = 'unknown'
                    per['ordinary_false_substitution_unknown'] += 1
                if outcome != 'unknown' and observed != expected and observed in forbidden:
                    per['ordinary_false_substitutions'] += 1
        counts[outcome] += 1
        per[outcome] += 1
    return {'status': 'ambiguous' if ambiguous else 'partial' if counts['unknown'] else 'scored',
        'counts': counts, 'categories': categories,
        'cer': None if ambiguous else {'errors': errors, 'reference_characters': len(normalized),
            'rate': errors / len(normalized) if normalized else None},
        'raw_retained_unmodified': True}


def evaluate_manifest(manifest, *, base_dir):
    """Return redacted lexical evidence; invalid or incomplete evidence is unknown."""
    report = {'schema_version': 1, 'normalization_version': NORMALIZATION,
        'normalization_unicode_version': unicodedata.unidata_version,
        'semantic_assessment': 'not_performed', 'provider_text_modified': False,
        'attestations': 'operator_declared_not_independently_proven', 'cases': []}
    try:
        _require(isinstance(manifest, dict) and type(manifest.get('schema_version')) is int
            and manifest['schema_version'] == 1, 'manifest_version_invalid')
        _require(manifest.get('normalization') == NORMALIZATION, 'normalization_invalid')
        _require(manifest.get('dataset_kind') in ('synthetic', 'human'), 'dataset_kind_missing')
        base_dir = Path(base_dir)
        audio, glossary = manifest.get('audio'), manifest.get('glossary')
        _file(audio, base_dir, limit=MAX_AUDIO_BYTES, text=False)
        _require(isinstance(glossary, dict) and _identity(glossary.get('source_version')),
            'glossary_identity_missing')
        _file(glossary, base_dir)
        cases = manifest.get('cases')
        _require(isinstance(cases, list) and 0 < len(cases) <= 256, 'cases_missing')
        report.update(audio_sha256=audio['sha256'], glossary_sha256=glossary['sha256'],
            glossary_source_version_sha256=hashlib.sha256(json.dumps(
                glossary['source_version'], ensure_ascii=False).encode('utf-8')).hexdigest(),
            dataset_kind=manifest['dataset_kind'], audio_format_validated=False)
        for case in cases:
            result = {'outputs': {}}
            report['cases'].append(result)
            try:
                _require(isinstance(case, dict), 'case_invalid')
                truth = case.get('truth_ref')
                _require(isinstance(truth, dict) and truth.get('human_reviewed') is True,
                    'human_reference_unverified')
                reference = _file(truth, base_dir)
                annotations = _annotations(case, reference)
                identities = [case.get(name) for name in OUTPUTS]
                _require(all(isinstance(value, dict) for value in identities), 'output_missing')
                _require(all(identities[0].get(key) == identities[1].get(key)
                    for key in ('provider', 'model')), 'paired_provider_mismatch')
                for name in OUTPUTS:
                    try:
                        output = _output(case[name], name, audio['sha256'], glossary, base_dir)
                        result['outputs'][name] = {**_score(reference, output, annotations),
                            'raw_sha256': case[name]['sha256'], 'reference_sha256': truth['sha256']}
                    except ContractError as error:
                        result['outputs'][name] = {'status': 'unknown', 'reason': str(error)}
                statuses = [value['status'] for value in result['outputs'].values()]
                result['status'] = ('scored' if all(value == 'scored' for value in statuses)
                    else 'unknown' if all(value == 'unknown' for value in statuses)
                    else 'ambiguous' if all(value == 'ambiguous' for value in statuses)
                    else 'partial')
            except ContractError as error:
                result.update(status='unknown', reason=str(error))
        statuses = [case['status'] for case in report['cases']]
        report['status'] = ('scored' if all(value == 'scored' for value in statuses)
            else 'unknown' if all(value == 'unknown' for value in statuses)
            else 'ambiguous' if all(value == 'ambiguous' for value in statuses)
            else 'partial')
    except ContractError as error:
        report.update(status='unknown', reason=str(error))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    # Reports, references and raw results belong in a private runtime directory.
    if args.output.resolve().is_relative_to(REPOSITORY):
        parser.error('report_must_be_outside_repository')
    try:
        raw = _private_bytes(args.manifest, MAX_TEXT_BYTES)
        manifest = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        manifest = None
    report = evaluate_manifest(manifest, base_dir=args.manifest.parent)
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except OSError:
        parser.error('report_write_failed')
    return 0 if report['status'] == 'scored' else 2


if __name__ == '__main__':
    raise SystemExit(main())
