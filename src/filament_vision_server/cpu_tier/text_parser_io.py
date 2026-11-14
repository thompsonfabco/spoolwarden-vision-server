"""Pickle-free serialization and inference for the CPU TF-IDF + SGD text parser."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse as sp
from sklearn.feature_extraction.text import TfidfVectorizer


class _ReconstructedTfidf:
    """Reconstructs a fitted TfidfVectorizer without pickle."""

    def __init__(
        self,
        vocabulary: dict[str, int],
        idf: np.ndarray,
        analyzer: str,
        ngram_range: tuple[int, int],
        sublinear_tf: bool = True,
    ) -> None:
        self._vec = TfidfVectorizer(
            analyzer=analyzer,
            ngram_range=ngram_range,
            sublinear_tf=sublinear_tf,
            vocabulary=vocabulary,
        )
        self._idf = np.asarray(idf, dtype=np.float64)
        self._n_features = len(self._idf)
        # Build analyzer function once
        self._analyze = self._vec.build_analyzer()

    def transform(self, texts: list[str]) -> sp.csr_matrix:
        rows: list[int] = []
        cols: list[int] = []
        vals: list[float] = []
        vocab = self._vec.vocabulary
        for row_idx, text in enumerate(texts):
            counts: dict[int, int] = {}
            for tok in self._analyze(text):
                col = vocab.get(tok)
                if col is not None:
                    counts[col] = counts.get(col, 0) + 1
            for col, cnt in counts.items():
                tf = 1.0 + np.log(cnt) if self._vec.sublinear_tf else float(cnt)
                rows.append(row_idx)
                cols.append(col)
                vals.append(tf * self._idf[col])
        mat = sp.csr_matrix(
            (np.asarray(vals, dtype=np.float64), (rows, cols)),
            shape=(len(texts), self._n_features),
            dtype=np.float64,
        )
        # L2 normalize each row (sklearn default norm='l2')
        norms = sp.linalg.norm(mat, axis=1)
        nz = norms > 0
        if np.any(nz):
            inv = np.zeros_like(norms)
            inv[nz] = 1.0 / norms[nz]
            mat = sp.diags(inv) @ mat
        return mat


class LinearTextPipeline:
    """Pickle-free equivalent of Pipeline(FeatureUnion([word, char]), SGDClassifier(loss='log_loss'))."""

    def __init__(
        self,
        word_vec: _ReconstructedTfidf,
        char_vec: _ReconstructedTfidf,
        coef: np.ndarray,
        intercept: np.ndarray,
        classes: list[str],
    ) -> None:
        self.word_vec = word_vec
        self.char_vec = char_vec
        self.coef_ = np.asarray(coef, dtype=np.float64)
        self.intercept_ = np.asarray(intercept, dtype=np.float64)
        self.classes_ = np.asarray(classes)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        xw = self.word_vec.transform(texts)
        xc = self.char_vec.transform(texts)
        x = sp.hstack([xw, xc], format="csr")
        scores = x @ self.coef_.T + self.intercept_
        # Match sklearn SGDClassifier._predict_proba_lr (OvR logistic)
        clipped = np.clip(scores, -30.0, 30.0)
        probs = 1.0 / (1.0 + np.exp(-clipped))
        if probs.shape[1] == 1:
            p1 = probs[:, 0]
            return np.column_stack([1.0 - p1, p1])
        row_sums = probs.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums > 0, row_sums, 1.0)
        return probs / row_sums


def save_text_parser_npz(bundle: dict[str, Any], out_path: str | Path) -> Path:
    """Export trained sklearn text parser pipelines to a pickle-free compressed .npz archive."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"format": "filament-vision-server-text-parser-v2", "pipes": {}}
    arrays: dict[str, np.ndarray] = {}

    for pipe_name in ("vendor_pipe", "material_pipe", "line_pipe"):
        pipe = bundle[pipe_name]
        feats = pipe.named_steps["feats"]
        clf = pipe.named_steps["clf"]
        word_tf: TfidfVectorizer = feats.transformer_list[0][1]
        char_tf: TfidfVectorizer = feats.transformer_list[1][1]

        meta["pipes"][pipe_name] = {
            "classes": [str(c) for c in clf.classes_],
            "word_vocab": {str(k): int(v) for k, v in word_tf.vocabulary_.items()},
            "word_ngram_range": list(word_tf.ngram_range),
            "word_sublinear_tf": bool(word_tf.sublinear_tf),
            "char_vocab": {str(k): int(v) for k, v in char_tf.vocabulary_.items()},
            "char_ngram_range": list(char_tf.ngram_range),
            "char_sublinear_tf": bool(char_tf.sublinear_tf),
        }
        arrays[f"{pipe_name}__word_idf"] = np.asarray(word_tf.idf_, dtype=np.float32)
        arrays[f"{pipe_name}__char_idf"] = np.asarray(char_tf.idf_, dtype=np.float32)
        arrays[f"{pipe_name}__coef"] = np.asarray(clf.coef_, dtype=np.float32)
        arrays[f"{pipe_name}__intercept"] = np.asarray(clf.intercept_, dtype=np.float32)

    meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    arrays["meta_utf8"] = np.frombuffer(meta_bytes, dtype=np.uint8)
    np.savez_compressed(out, **arrays)
    return out


def load_text_parser_npz(path: str | Path) -> dict[str, LinearTextPipeline]:
    """Load a pickle-free text_parser.npz archive using allow_pickle=False."""
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(bytes(data["meta_utf8"].tolist()).decode("utf-8"))
        out: dict[str, LinearTextPipeline] = {}
        for pipe_name, pmeta in meta["pipes"].items():
            word_vec = _ReconstructedTfidf(
                vocabulary=pmeta["word_vocab"],
                idf=data[f"{pipe_name}__word_idf"],
                analyzer="word",
                ngram_range=tuple(pmeta["word_ngram_range"]),
                sublinear_tf=bool(pmeta.get("word_sublinear_tf", True)),
            )
            char_vec = _ReconstructedTfidf(
                vocabulary=pmeta["char_vocab"],
                idf=data[f"{pipe_name}__char_idf"],
                analyzer="char_wb",
                ngram_range=tuple(pmeta["char_ngram_range"]),
                sublinear_tf=bool(pmeta.get("char_sublinear_tf", True)),
            )
            out[pipe_name] = LinearTextPipeline(
                word_vec=word_vec,
                char_vec=char_vec,
                coef=data[f"{pipe_name}__coef"],
                intercept=data[f"{pipe_name}__intercept"],
                classes=pmeta["classes"],
            )
        return out
