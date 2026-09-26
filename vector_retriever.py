# -*- coding: utf-8 -*-
"""向量检索后端（任务 A 新增，最小实现，零重框架）。

设计原则与 `rag_core.py` 一致：**每行都能讲清**。
  - 不引入 LangChain / sentence-transformers / torch / 任何在线 embedding API
  - 只用 4 个依赖：numpy（矩阵）、faiss-cpu（IndexFlatIP）、onnxruntime（推理）、
    tokenizers（BERT 分词）。旧仓库 `rag-knowledge-base` 的切片、`score > 0.3`
    阈值、API 失败自动切本地模型的逻辑**一概不移植**。

与 BM25 的可比口径（对照实验成立的前提）：
  - **切片不动**：输入是 `rag_core.load_documents(corpus_id)` 产出的同一批 chunk，
    本模块只做「读 chunk → 编码 → 排序」，不碰 text / section / id。
  - **检索接口同构**：`search(query, k) -> [(score, doc), ...]`，与
    `rag_core.BM25.search` 完全同形，评测层可以原样复用同一套判据。
  - **输入字段**：默认 `search_text`（含标题与节路径），与 BM25 用同一字段。
  - **查询端不加任何指令前缀**：题面原样送入。注意该模型官方对检索类查询建议加
    一句中文指令前缀（s2p 用法），本轮**刻意不用**，属于已知偏差，已在报告里声明；
    加前缀属于另一轮实验，不能混进本轮指标。
  - **不设相似度阈值**：Top-K 之外不做取舍。旧仓库那个 `0.3` 会直接改变 Hit@K，
    且有"阈值是拍出来的、没有校准集"的问题。

向量怎么来（面试可复述）：
  1. 文本 → BERT WordPiece 分词（Xenova/bge-small-zh-v1.5 的 tokenizer.json）
     → input_ids / attention_mask / token_type_ids
  2. ONNX Runtime 跑一次前向 → last_hidden_state（每个 token 一个 512 维向量）
  3. 取 **第 0 个 token（[CLS]）** 那一个向量作为整句表示（池化方式由官方
     `1_Pooling/config.json` 声明：pooling_mode_cls_token = true）
  4. L2 归一化：`v / ||v||`
  5. 归一化后内积 = 余弦相似度，所以用 `faiss.IndexFlatIP` 就是精确的余弦检索
     （暴力检索，无近似、无训练；471 条向量不需要 IVF/HNSW）

模型从哪里来：环境变量 `VECTOR_MODEL_DIR` 指向本地已落盘的模型目录（默认
`<项目>/models/bge-small-zh-v1.5-onnx`）。模型身份记在 `model_manifest` 里：
仓库名 + commit sha + 每个文件的 sha256，避免"换了个模型还叫同名指标"。
"""
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
INDEX_DIR = BASE_DIR / "index"

MODEL_DIR_ENV = "VECTOR_MODEL_DIR"
DEFAULT_MODEL_DIR = BASE_DIR / "models" / "bge-small-zh-v1.5-onnx"

MODEL_ID = "Xenova/bge-small-zh-v1.5@onnx"      # transformers.js 的 ONNX 导出
BASE_MODEL = "BAAI/bge-small-zh-v1.5"
POOLING = "cls"
NORMALIZE = "L2"
MAX_LEN = 512
DEFAULT_FIELD = "search_text"
QUERY_TEMPLATE = "none"                          # 查询端不加指令前缀（见模块 docstring）
BATCH_SIZE = 16
CACHE_VERSION = 1


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_model_dir(model_dir=None) -> Path:
    """模型目录解析顺序：显式参数 → 环境变量 → 项目内默认位置。"""
    cand = Path(model_dir) if model_dir else Path(os.environ.get(MODEL_DIR_ENV) or DEFAULT_MODEL_DIR)
    if not cand.exists():
        raise FileNotFoundError(
            f"找不到本地 embedding 模型目录：{cand}\n"
            f"（本机用环境变量 {MODEL_DIR_ENV} 指向已固定版本的 ONNX 模型目录；"
            "本模块不做任何自动下载与降级）"
        )
    return cand


class BgeOnnxEncoder:
    """固定模型 → 固定维度向量；任何不一致都直接失败，绝不静默降级。"""

    def __init__(self, model_dir=None):
        from tokenizers import Tokenizer
        import onnxruntime as ort

        self.model_dir = resolve_model_dir(model_dir)
        cfg = json.loads((self.model_dir / "config.json").read_text(encoding="utf-8"))
        self.dim = int(cfg["hidden_size"])
        self.max_position_embeddings = int(cfg["max_position_embeddings"])
        self.max_len = min(MAX_LEN, self.max_position_embeddings)

        onnx_path = self.model_dir / "model.onnx"
        if not onnx_path.exists():                                  # 兼容下载时的扁平命名
            onnx_path = self.model_dir / "onnx__model.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"模型目录里没有 model.onnx：{self.model_dir}")
        self.onnx_path = onnx_path
        tok_path = self.model_dir / "tokenizer.json"
        if not tok_path.exists():
            raise FileNotFoundError(f"模型目录里没有 tokenizer.json：{self.model_dir}")
        self.tokenizer_path = tok_path

        self.tokenizer = Tokenizer.from_file(str(tok_path))
        self.tokenizer.enable_truncation(max_length=self.max_len)
        self.tokenizer.no_padding()

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        self.session = ort.InferenceSession(str(onnx_path), so,
                                           providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.output_names = [o.name for o in self.session.get_outputs()]
        expected = {"input_ids", "attention_mask"}
        if not expected.issubset(set(self.input_names)):
            raise RuntimeError(f"ONNX 输入与预期不符：{self.input_names}")

    # ---- 身份：换模型 / 换分词器 / 换池化，缓存都必须失效 ----
    def identity(self) -> dict:
        return {
            "model_id": MODEL_ID,
            "base_model": BASE_MODEL,
            "onnx_sha256": sha256_file(self.onnx_path),
            "tokenizer_sha256": sha256_file(self.tokenizer_path),
            "dim": self.dim,
            "pooling": POOLING,
            "normalize": NORMALIZE,
            "max_len": self.max_len,
            "query_template": QUERY_TEMPLATE,
        }

    def token_lengths(self, texts: list) -> list:
        return [len(e.ids) for e in self.tokenizer.encode_batch(list(texts))]

    def _encode_batch(self, texts: list) -> np.ndarray:
        encodings = self.tokenizer.encode_batch(list(texts))
        n = len(encodings)
        width = max(len(e.ids) for e in encodings)
        ids = np.zeros((n, width), dtype=np.int64)
        mask = np.zeros((n, width), dtype=np.int64)
        type_ids = np.zeros((n, width), dtype=np.int64)
        for row, e in enumerate(encodings):
            used = len(e.ids)
            ids[row, :used] = e.ids
            mask[row, :used] = e.attention_mask
            type_ids[row, :used] = e.type_ids

        feed = {}
        for name in self.input_names:
            if name == "input_ids":
                feed[name] = ids
            elif name == "attention_mask":
                feed[name] = mask
            elif name == "token_type_ids":
                feed[name] = type_ids
            else:
                raise RuntimeError(f"模型出现未预期输入：{name}")

        hidden = self.session.run([self.output_names[0]], feed)[0]     # (n, seq, dim)
        if hidden.shape[0] != n or hidden.shape[2] != self.dim:
            raise RuntimeError(f"ONNX 输出形状异常：{hidden.shape}（期望 ({n}, seq, {self.dim})）")
        vec = hidden[:, 0, :].astype(np.float32)                      # [CLS] 池化
        if not np.isfinite(vec).all():
            raise RuntimeError("向量中出现 NaN / inf，拒绝继续")
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        return vec / np.maximum(norm, 1e-12)                          # L2 归一化

    def encode(self, texts: list, batch_size: int = BATCH_SIZE) -> np.ndarray:
        if not texts:
            raise ValueError("encode() 收到空列表")
        parts = [self._encode_batch(texts[i:i + batch_size])
                 for i in range(0, len(texts), batch_size)]
        return np.vstack(parts)


class VectorRetriever:
    """向量检索器：与 `rag_core.BM25` 同形的 `search(query, k)`。

    缓存身份 = 语料指纹 + chunk id 顺序 + 输入字段 + 模型身份。
    任何一项不一致，`load_cache()` 会抛 `ValueError`，调用方必须重建索引。
    """

    def __init__(self, docs: list, corpus_id: str, corpus_fingerprint: str,
                 encoder: "BgeOnnxEncoder | None" = None, field: str = DEFAULT_FIELD,
                 model_dir=None, cache_dir=None):
        self.docs = docs
        self.corpus_id = corpus_id
        self.corpus_fingerprint = corpus_fingerprint
        self.field = field
        self.enc = encoder or BgeOnnxEncoder(model_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else INDEX_DIR
        self.vectors = None
        self.index = None
        self.build_seconds = None

    # ---------------- 身份与缓存 ----------------
    def identity(self) -> dict:
        return {
            "cache_version": CACHE_VERSION,
            "corpus_id": self.corpus_id,
            "corpus_fingerprint": self.corpus_fingerprint,
            "chunk_count": len(self.docs),
            "chunk_ids_sha256": _sha256_text("\n".join(d["id"] for d in self.docs)),
            "field": self.field,
            **self.enc.identity(),
        }

    def cache_paths(self):
        stem = f"vector_{self.corpus_id}_{self.field}"
        return self.cache_dir / f"{stem}.meta.json", self.cache_dir / f"{stem}.npy"

    def save_cache(self) -> Path:
        meta_path, vec_path = self.cache_paths()
        if self.vectors is None:
            raise RuntimeError("还没有向量可存：先 build() 或 load_cache()")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(vec_path, self.vectors)
        meta_path.write_text(json.dumps({
            "identity": self.identity(),
            "vectors_file": vec_path.name,
            "vectors_sha256": sha256_file(vec_path),
            "rows": int(self.vectors.shape[0]),
            "dim": int(self.vectors.shape[1]),
            "note": "身份里任何一项变化都会使本缓存失效（load_cache 会拒绝）",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta_path

    def load_cache(self) -> Path:
        """身份一致才复用；不一致直接抛错，不静默重建也不静默复用。"""
        meta_path, vec_path = self.cache_paths()
        if not meta_path.exists() or not vec_path.exists():
            raise FileNotFoundError(f"没有缓存：{meta_path} / {vec_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        got, want = meta.get("identity", {}), self.identity()
        diff = {k: (got.get(k), want.get(k)) for k in set(got) | set(want)
                if got.get(k) != want.get(k)}
        if diff:
            raise ValueError(f"CACHE_IDENTITY_MISMATCH：{sorted(diff)}")
        if sha256_file(vec_path) != meta.get("vectors_sha256"):
            raise ValueError("CACHE_VECTORS_TAMPERED：缓存向量文件与声明哈希不符")
        self.vectors = np.load(vec_path)
        if self.vectors.shape != (len(self.docs), self.enc.dim):
            raise ValueError(f"CACHE_SHAPE_MISMATCH：{self.vectors.shape} "
                             f"≠ ({len(self.docs)}, {self.enc.dim})")
        self._build_index()
        return meta_path

    # ---------------- 建索引与检索 ----------------
    def build(self, use_cache: bool = True) -> "VectorRetriever":
        """有可用缓存就复用（校验身份），否则重建；**重建原因要留痕**，不静默。"""
        self.cache_status = "built(no cache requested)"
        if use_cache:
            try:
                self.load_cache()
                self.from_cache = True
                self.cache_status = "cache_hit"
                return self
            except (FileNotFoundError, ValueError) as exc:
                self.from_cache = False
                self.cache_status = f"rebuilt: {type(exc).__name__}: {str(exc)[:140]}"
        t0 = time.time()
        self.vectors = self.enc.encode([d[self.field] for d in self.docs])
        self.build_seconds = round(time.time() - t0, 2)
        self._build_index()
        self.save_cache()          # 存下身份与向量，下次先校验身份再复用
        return self

    def _build_index(self):
        import faiss
        index = faiss.IndexFlatIP(self.enc.dim)      # 归一化向量 → 内积即余弦
        index.add(np.ascontiguousarray(self.vectors))
        self.index = index

    def search(self, query: str, k: int = 5) -> list:
        """返回 [(余弦分数, 原 chunk dict), ...]，按分数降序；与 BM25.search 同形。"""
        if self.index is None:
            raise RuntimeError("索引未就绪：先 build() 或 load_cache()")
        qv = np.ascontiguousarray(self.enc.encode([query]))
        scores, idx = self.index.search(qv, k)
        return [(float(s), self.docs[int(i)]) for s, i in zip(scores[0], idx[0]) if i >= 0]


def build_vector_retriever(corpus_id: str, docs: list, corpus_fingerprint: str,
                           field: str = DEFAULT_FIELD, model_dir=None,
                           use_cache: bool = True) -> VectorRetriever:
    """一行拿到可检索的向量后端（评测入口与将来界面都用这一个入口）。"""
    retriever = VectorRetriever(docs, corpus_id, corpus_fingerprint,
                                field=field, model_dir=model_dir)
    retriever.build(use_cache=use_cache)
    return retriever
