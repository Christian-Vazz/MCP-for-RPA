"""Embeddings. Padrão: vetorização por hashing (offline, sem downloads, ~microssegundos).

Como funciona o HashEmbedder: cada palavra (e par de palavras) é mapeada por hash para uma das
`dim` posições do vetor, com sinal ±1 (reduz colisões). O peso é 1 + log(frequência). O vetor é
normalizado, então o produto escalar entre dois textos = similaridade de cosseno.
É uma busca lexical robusta; para semântica real use EMBEDDING_BACKEND=st.
"""
import logging
import math
import re
import threading
import unicodedata
import zlib
from collections import Counter

import numpy as np

from apps.conf import cfg

log = logging.getLogger(__name__)

_STOP = set("""a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sob sobre e ou mas que se ao aos
 é são foi ser ter tem há como mais já não the of and to in is it for on with as at by an be this that are or from""".split())
_WORD = re.compile(r"[a-z0-9]{2,}")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str):
    out = []
    for t in _WORD.findall(_normalize(text)):
        if t in _STOP:
            continue
        if len(t) > 4 and t.endswith("s"):  # plural simples
            t = t[:-1]
        out.append(t)
    return out


class HashEmbedder:
    name = "hash"

    def __init__(self, dim: int):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        toks = _tokens(text)
        feats = Counter(toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])])
        v = np.zeros(self.dim, dtype=np.float32)
        for feat, count in feats.items():
            raw = feat.encode()
            idx = zlib.crc32(raw) % self.dim
            sign = 1.0 if (zlib.adler32(raw) & 1) else -1.0
            weight = (1.0 + math.log(count)) * (0.6 if "_" in feat else 1.0)
            v[idx] += sign * weight
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def embed(self, texts):
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts]).astype(np.float32)


class SentenceTransformerEmbedder:
    name = "st"

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # import tardio (dependência opcional)
        self.model = SentenceTransformer(model_name)
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def embed(self, texts):
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.asarray(self.model.encode(texts, normalize_embeddings=True), dtype=np.float32)


_lock = threading.Lock()
_instance = None
_instance_key = None


def get_embedder():
    global _instance, _instance_key
    key = (cfg("EMBEDDING_BACKEND"), cfg("EMBEDDING_MODEL"), cfg("EMBEDDING_DIM"))
    with _lock:
        if _instance is None or _instance_key != key:
            if key[0] == "st":
                try:
                    _instance = SentenceTransformerEmbedder(key[1])
                except Exception as exc:  # noqa: BLE001
                    log.warning("sentence-transformers indisponível (%s); usando hash.", exc)
                    _instance = HashEmbedder(key[2])
            else:
                _instance = HashEmbedder(key[2])
            _instance_key = key
        return _instance
