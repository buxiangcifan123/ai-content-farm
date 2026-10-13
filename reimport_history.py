"""
历史数据重入库 — 把 6/8 ~ 7/2 的 *_raw.json + *_external.json 重新入库到 content_farm
用法: python reimport_history.py
"""
import hashlib
import json
import sys
import io
from datetime import datetime
from pathlib import Path

# Windows UTF-8 fix
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import chromadb
import urllib.request

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
OUTPUT_DIR = ROOT / "output"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

CHROMA_PATH = Path(config["chromadb"]["path"])
EMBED_MODEL = config["chromadb"]["embedding_model"]
OLLAMA_BATCH_URL = "http://localhost:11434/api/embed"
BATCH_SIZE = 50

# 排除 7/3 — 已在库里
EXCLUDE_DATES = {"20260703"}

def get_client():
    return chromadb.PersistentClient(path=str(CHROMA_PATH))

def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def load_existing_urls(collection):
    """读取已在库里的 URL，用于去重"""
    existing = set()
    try:
        all_data = collection.get(include=["metadatas"])
        for meta in all_data["metadatas"]:
            url = meta.get("url", "")
            if url:
                existing.add(url)
    except Exception:
        pass
    return existing

def embed_batch(texts):
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_BATCH_URL, data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["embeddings"]

def main():
    print("=" * 50)
    print("  历史数据重入库 — 追回 6/8 ~ 7/2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 收集所有历史 JSON 文件
    raw_files = list(OUTPUT_DIR.glob("[0-9]*_raw.json"))
    ext_files = list(OUTPUT_DIR.glob("[0-9]*_external.json"))
    all_files = sorted(raw_files + ext_files)

    # 排除今天（已在库里）
    to_process = [f for f in all_files
                  if f.stem.split("_")[0] not in EXCLUDE_DATES]

    print(f"\n📂 找到 {len(all_files)} 个文件，排除今天后待处理 {len(to_process)} 个\n")

    if not to_process:
        print("📭 无历史数据需要处理")
        return

    client = get_client()
    collection = client.get_or_create_collection(
        name="content_farm", metadata={"hnsw:space": "cosine"},
    )

    # 读取已存在的 URL 用于去重
    existing_urls = load_existing_urls(collection)
    print(f"🔍 库中已有 {len(existing_urls)} 条记录的 URL\n")

    all_texts, all_hashes, all_metas = [], [], []
    skipped_url = 0
    skipped_empty = 0
    file_stats = []

    for f in to_process:
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠️ 读取失败 {f.name}: {e}")
            continue

        # raw.json 是列表，external.json 也是列表
        if not isinstance(items, list):
            items = [items]

        file_add = 0
        file_skip = 0
        for item in items:
            title = item.get("title", "") or ""
            content = item.get("content", "") or item.get("summary", "") or ""
            url = item.get("url", "") or item.get("link", "") or ""

            combined = f"{title}\n\n{content}" if content else title
            if len(combined.strip()) < 10:
                skipped_empty += 1
                file_skip += 1
                continue

            # URL 去重
            if url and url in existing_urls:
                skipped_url += 1
                file_skip += 1
                continue

            h = content_hash(combined)
            all_texts.append(combined[:1000])
            all_hashes.append(h)
            all_metas.append({
                "title": title,
                "content": content[:500],
                "url": url,
                "source": item.get("source", ""),
                "source_type": item.get("source_type", "content-farm"),
                "fetched_at": item.get("fetched_at", f.stem.split("_")[0]),
            })
            existing_urls.add(url)  # 避免同批次内重复
            file_add += 1

        date = f.stem.split("_")[0]
        file_stats.append((date, file_add, file_skip))
        print(f"  {f.name}: +{file_add} 条, 跳过 {file_skip} 条")

    if not all_texts:
        print("\n📭 全部重复，无新增数据")
        return

    print(f"\n📊 待入库总计: {len(all_texts)} 条")
    print(f"   跳过(URL重复): {skipped_url}")
    print(f"   跳过(内容为空): {skipped_empty}")

    # 分批嵌入入库
    added = 0
    for i in range(0, len(all_texts), BATCH_SIZE):
        batch_texts = all_texts[i:i + BATCH_SIZE]
        batch_ids = all_hashes[i:i + BATCH_SIZE]
        batch_metas = all_metas[i:i + BATCH_SIZE]
        try:
            embeddings = embed_batch(batch_texts)
            collection.add(ids=batch_ids, embeddings=embeddings,
                          documents=batch_texts, metadatas=batch_metas)
            added += len(batch_texts)
            print(f"  📦 入库: {added}/{len(all_texts)}")
        except Exception as e:
            print(f"  ⚠️ 批次入库失败: {e}")
            continue

    print(f"\n{'=' * 50}")
    print(f"✅ 完成！新增入库 {added} 条历史数据到 content_farm")
    print(f"   当前集合总文档数: {collection.count()}")
    print(f"{'=' * 50}")

if __name__ == "__main__":
    main()
