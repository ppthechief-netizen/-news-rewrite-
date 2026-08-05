from typing import Tuple

from rapidfuzz.distance import JaroWinkler
from simhash import Simhash


def simhash_value(text: str) -> int:
    tokens = [t for t in text.split() if len(t) > 2]
    return Simhash(tokens).value


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def ngram_overlap(src: str, gen: str, n: int = 10) -> float:
    def ngrams(t, n):
        toks = t.split()
        return {" ".join(toks[i : i + n]) for i in range(0, max(0, len(toks) - n + 1))}

    A = ngrams(src, n)
    B = ngrams(gen, n)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A))


def originality_metrics(source: str, draft: str) -> Tuple[float, int, float]:
    s1 = simhash_value(source)
    s2 = simhash_value(draft)
    ham = hamming_distance(s1, s2)
    overlap10 = ngram_overlap(source, draft, n=10)
    jaro = 1.0 - JaroWinkler.normalized_distance(source, draft)
    return overlap10, ham, jaro


def needs_more_paraphrase(source: str, draft: str) -> bool:
    overlap10, ham, jaro = originality_metrics(source, draft)
    # Triggers if long-phrase overlap >= 0.02 OR SimHash is too similar (<=14 bits differ) OR Jaro similarity very high
    return (overlap10 >= 0.02) or (ham <= 14) or (jaro >= 0.92)
