"""
FAISS + SQLite 向量存储引擎。
替换 ChromaDB（Python 1.5.9 Rust HNSW 有 bug，大集合写坏索引用不了）。
"""
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import faiss
import numpy as np

# ── 配置 ──────────────────────────────────────────────────────
DIM = 768  # nomic-embed-text 维度
# ⚠️ FAISS C++ 不支持中文路径，必须用纯 ASCII 路径
INDEX_DIR = Path(os.environ.get("FAISS_INDEX_DIR", str(Path.home() / ".ai-faiss-index")))
DB_PATH = str(INDEX_DIR / "vector_store.db")


# ── 工具 ──────────────────────────────────────────────────────
def _init_db():
    """确保 SQLite 表存在。"""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id TEXT NOT NULL REFERENCES collections(id),
            doc_id TEXT NOT NULL,
            text TEXT,
            metadata TEXT DEFAULT '{}',
            UNIQUE(collection_id, doc_id)
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            doc_row_id INTEGER PRIMARY KEY REFERENCES documents(id),
            vector BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_docs_collection ON documents(collection_id);
    """)
    conn.commit()
    conn.close()


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化（cosine similarity 用 IP 搜索时需归一化）。"""
    vecs = vecs.astype(np.float32)
    faiss.normalize_L2(vecs)
    return vecs


# ── 集合包装器（兼容 ChromaDB Collection API） ────────────────
class FaissCollection:
    """类似 ChromaDB Collection，背后是 FAISS 索引 + SQLite。"""

    def __init__(self, name: str):
        self.name = name
        self._index_path = INDEX_DIR / f"{name}.faiss"
        self._index: Optional[faiss.Index] = None
        self._load_index()

    def _load_index(self):
        if self._index_path.exists():
            try:
                self._index = faiss.read_index(str(self._index_path))
                return
            except Exception:
                pass
        # 新建空索引
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))

    def _save_index(self):
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._index_path))

    def _col_id(self, conn) -> str:
        cur = conn.execute("SELECT id FROM collections WHERE name = ?", (self.name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cid = str(uuid.uuid4())
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (cid, self.name))
        conn.commit()
        return cid

    # ── 公共 API ────────────────────────────────────────────

    def count(self) -> int:
        conn = sqlite3.connect(DB_PATH)
        cid = self._col_id(conn)
        row = conn.execute("SELECT COUNT(*) FROM documents WHERE collection_id = ?", (cid,)).fetchone()
        conn.close()
        return row[0] if row else 0

    def get(self, include: Optional[list[str]] = None, limit: Optional[int] = None,
            ids: Optional[list[str]] = None):
        """返回 {ids, metadatas, documents, embeddings}。"""
        conn = sqlite3.connect(DB_PATH)
        cid = self._col_id(conn)
        sql = "SELECT doc_id, text, metadata FROM documents WHERE collection_id = ?"
        params: list[Any] = [cid]
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            sql += f" AND doc_id IN ({placeholders})"
            params.extend(ids)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()

        ids = []
        metadatas = []
        documents = []
        for doc_id, text, meta_json in rows:
            ids.append(doc_id)
            metadatas.append(json.loads(meta_json))
            documents.append(text or "")
        result: dict[str, Any] = {"ids": ids, "metadatas": metadatas, "documents": documents}
        if include and "embeddings" in include:
            result["embeddings"] = []
        return result

    def add(self, ids: list[str], embeddings: list[list[float]], documents: list[str], metadatas: list[dict]):
        """增量添加文档 + 向量。"""
        if not ids:
            return

        vecs = _normalize(np.array(embeddings, dtype=np.float32))
        conn = sqlite3.connect(DB_PATH)
        cid = self._col_id(conn)

        new_faiss_ids: list[int] = []
        new_vecs_list: list[np.ndarray] = []

        for i, doc_id in enumerate(ids):
            text = documents[i] if i < len(documents) else ""
            meta = metadatas[i] if i < len(metadatas) else {}
            conn.execute(
                "INSERT OR IGNORE INTO documents (collection_id, doc_id, text, metadata) VALUES (?, ?, ?, ?)",
                (cid, doc_id, text, json.dumps(meta, ensure_ascii=False)),
            )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO embeddings (doc_row_id, vector) VALUES (?, ?)",
                    (row_id, vecs[i].tobytes()),
                )
                new_faiss_ids.append(row_id)
                new_vecs_list.append(vecs[i])

        conn.commit()
        conn.close()

        if new_faiss_ids:
            self._index.add_with_ids(
                np.array(new_vecs_list, dtype=np.float32),
                np.array(new_faiss_ids, dtype=np.int64),
            )
            self._save_index()

    def query(self, query_embeddings: list[list[float]], n_results: int = 10,
              include: Optional[list[str]] = None):
        """语义搜索。"""
        qvec = _normalize(np.array(query_embeddings, dtype=np.float32))
        n = min(n_results, self._index.ntotal)
        if n == 0:
            return {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}

        distances, indices = self._index.search(qvec, n)

        conn = sqlite3.connect(DB_PATH)
        result_ids = []
        result_docs = []
        result_metas = []
        result_dists = []

        for batch_idx in range(len(distances)):
            batch_ids = []
            batch_docs = []
            batch_metas = []
            batch_dists = []
            for rank_idx in range(len(distances[batch_idx])):
                faiss_id = int(indices[batch_idx][rank_idx])
                # FAISS IP 返回 cosine similarity，转成 ChromaDB 兼容的 cosine distance (1 - sim)
                dist = 1.0 - float(distances[batch_idx][rank_idx])
                if faiss_id < 0:
                    continue
                row = conn.execute(
                    "SELECT d.doc_id, d.text, d.metadata FROM documents d WHERE d.id = ?",
                    (faiss_id,),
                ).fetchone()
                if row:
                    batch_ids.append(row[0])
                    batch_docs.append(row[1] or "")
                    batch_metas.append(json.loads(row[2]))
                    batch_dists.append(dist)
            result_ids.append(batch_ids)
            result_dists.append(batch_dists)
            result_docs.append(batch_docs)
            result_metas.append(batch_metas)

        conn.close()
        out: dict[str, Any] = {
            "ids": result_ids,
            "distances": result_dists,
            "documents": result_docs,
            "metadatas": result_metas,
        }
        return out


# ── 客户端（兼容 ChromaDB PersistentClient API） ──────────────
class FaissClient:
    """替代 chromadb.PersistentClient / HttpClient。"""

    def __init__(self, path: str = ""):
        _init_db()

    def get_collection(self, name: str) -> FaissCollection:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"Collection '{name}' does not exist")
        return FaissCollection(name)

    def get_or_create_collection(self, name: str, metadata: Optional[dict] = None) -> FaissCollection:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
        if not row:
            cid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO collections (id, name, metadata) VALUES (?, ?, ?)",
                (cid, name, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.commit()
        conn.close()
        return FaissCollection(name)

    def create_collection(self, name: str, metadata: Optional[dict] = None) -> FaissCollection:
        conn = sqlite3.connect(DB_PATH)
        try:
            cid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO collections (id, name, metadata) VALUES (?, ?, ?)",
                (cid, name, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return FaissCollection(name)

    def list_collections(self) -> list[FaissCollection]:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT name FROM collections ORDER BY name").fetchall()
        conn.close()
        return [FaissCollection(r[0]) for r in rows]

    def delete_collection(self, name: str):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM embeddings WHERE doc_row_id IN (SELECT id FROM documents WHERE collection_id = (SELECT id FROM collections WHERE name = ?))", (name,))
        conn.execute("DELETE FROM documents WHERE collection_id = (SELECT id FROM collections WHERE name = ?)", (name,))
        conn.execute("DELETE FROM collections WHERE name = ?", (name,))
        conn.commit()
        conn.close()
        idx_path = INDEX_DIR / f"{name}.faiss"
        if idx_path.exists():
            idx_path.unlink()


def get_client() -> FaissClient:
    """获取 FAISS 客户端（替换 chroma_client 的 get_client）。"""
    return FaissClient()
