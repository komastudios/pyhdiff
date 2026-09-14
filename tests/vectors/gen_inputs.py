"""Regenerate the synthetic (base, target) inputs of these interop vectors.

Deterministic for seed 20260914 with CPython's random module.
Usage: python3 gen_inputs.py OUTPUT_DIRECTORY
"""
import random
import sys
from pathlib import Path

SEED = 20260914


def record(rng, i, value):
    return ('{"key":"item-%07d","state":"%s","size":%d,"group":"g%03d",'
            '"updated":"2026-09-14T%02d:%02d:%02dZ","attrs":{"a":"%06d","b":"%06d"}}'
            % (i, value, rng.choice((11, 22, 50, 150, 350)), rng.randrange(1000),
               rng.randrange(24), rng.randrange(60), rng.randrange(60), rng.randrange(10**6), rng.randrange(10**6)))


def document(n, seed):
    rng = random.Random(seed)
    states = ('alpha', 'beta', 'gamma', 'delta', 'epsilon')
    return [record(rng, i, rng.choice(states)) for i in range(n)]


def join(records):
    return ('[' + ',\n'.join(records) + ']\n').encode()


def pairs():
    out = []
    for label, n in (('small', 400), ('medium', 6000), ('large', 95000)):
        base = document(n, SEED + n)
        rng = random.Random(SEED + 7 * n)
        edit = list(base)
        for j in rng.sample(range(n), max(1, n // 200)):
            edit[j] = edit[j].replace('alpha', 'beta') if 'alpha' in edit[j] else edit[j].replace('"state":"', '"state":"x')
        out.append((label + '-sparse-edits', join(base), join(edit)))
        churn = list(base)
        for j in rng.sample(range(n), n // 5):
            churn[j] = record(rng, j, 'beta')
        cut = n // 3
        churn = churn[:cut] + [record(rng, n + k, 'alpha') for k in range(n // 50)] + churn[cut + n // 50:]
        out.append((label + '-bulk-churn', join(base), join(churn)))
    base = join(document(400, SEED))
    out.append(('identical', base, base))
    out.append(('unrelated', join(document(400, SEED + 1)), random.Random(SEED + 2).randbytes(len(base))))
    out.append(('empty-base-and-small-target', b'', join(document(20, SEED + 3))))
    return out


if __name__ == '__main__':
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    for label, base, target in pairs():
        (directory / f'{label}.base').write_bytes(base)
        (directory / f'{label}.target').write_bytes(target)
