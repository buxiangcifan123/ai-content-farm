"""AI 信息差搬运工 — 每日一条精品内容管线"""
import json
import os
import re
import subprocess
import sys
import time
import io

# Windows UTF-8 修复
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import webbrowser
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"

# ChromaDB 客户端（统一走 PersistentClient）
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from chroma_client import get_client  # noqa: E402
OUTPUT_DIR = ROOT / "output"
IMAGES_DIR = ROOT / "images"
OUTPUT_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _setup_proxy(config: dict):
    """安装全局代理，让 urllib / feedparser 都走代理"""
    proxy_url = config.get("proxy", "")
    if not proxy_url:
        return
    # 设置环境变量（feedparser 依赖这些）
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    # 安装 urllib ProxyHandler
    proxy_handler = urllib.request.ProxyHandler({
        "http": proxy_url,
        "https": proxy_url,
    })
    opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(opener)
    print(f"🔌 代理已设置: {proxy_url}")


def fetch_rss(url: str) -> list:
    """抓取 RSS 源，返回 [{'title':..., 'link':..., 'summary':...}]"""
    import feedparser
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:10]:
            items.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": re.sub(r'<[^>]+>', '', entry.get("summary", "")),
                "source": url.split("/")[2]  # domain
            })
        return items
    except Exception as e:
        print(f"  ⚠️ RSS 抓取失败 ({url[:50]}): {e}")
        return []


def fetch_reddit(subreddits: list) -> list:
    """通过 old.reddit.com RSS 抓取多个子版块（合并为一个请求，避免 429 限流）。
    返回 [{'title':..., 'link':..., 'summary':..., 'source':...}]"""
    import feedparser
    if not subreddits:
        return []
    # 合并为一个 RSS URL: r/sub1+sub2+sub3/.rss
    joined = "+".join(subreddits)
    url = f"https://old.reddit.com/r/{joined}/.rss?limit=100"
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            summary = re.sub(r'<[^>]+>', '', entry.get("summary", ""))
            # 跳过置顶帖和每日讨论帖
            if any(kw in title.lower() for kw in ["daily discussion", "weekly thread", "megathread"]):
                continue
            # 从 link 中提取子版块名
            m = re.search(r'/r/(\w+)/', link)
            sub_name = m.group(1) if m else "reddit"
            items.append({
                "title": title,
                "link": link,
                "summary": summary[:300] if summary else title,
                "source": f"reddit-{sub_name}",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ Reddit 抓取失败: {e}")
        return []


def fetch_github_trending() -> list:
    """抓取 GitHub Trending（不需要 VPN）"""
    import urllib.request
    try:
        url = "https://api.github.com/search/repositories?q=stars:>100&sort=stars&order=desc&per_page=10"
        req = urllib.request.Request(url, headers={"User-Agent": "AI-Content-Farm", "Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        items = []
        for repo in data.get("items", []):
            desc = repo.get("description") or ""
            items.append({
                "title": f"{repo['full_name']} — ⭐{repo['stargazers_count']}",
                "link": repo["html_url"],
                "summary": desc,
                "source": "github.com"
            })
        return items
    except Exception as e:
        print(f"  ⚠️ GitHub API 失败: {e}")
        return []


def build_history_urls() -> set:
    """读取历史输出 JSON，提取所有已选 URL 用于去重"""
    import re
    history_urls = set()

    for json_file in sorted(OUTPUT_DIR.glob("[0-9]*[0-9].json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            url = data.get("pick_url", "")
            if not url:
                raw_text = json.dumps(data, ensure_ascii=False)
                found_urls = re.findall(r'https?://[^\s"\'\\]+', raw_text)
                if found_urls:
                    url = found_urls[0]
                    print(f"  🔧 从 {json_file.name} 提取到 URL: {url[:60]}...")
            if url:
                history_urls.add(url)
        except Exception:
            pass

    return history_urls


def dedup_items(items: list, history_urls: set) -> list:
    """根据历史 URL 去重"""
    before = len(items)
    filtered = [it for it in items if it.get("link", "") not in history_urls]
    if before != len(filtered):
        print(f"🗑 URL 去重：{before} → {len(filtered)} 条（排除 {len(history_urls)} 条历史已选）")
    return filtered


def fetch_from_vectordb(config: dict) -> list:
    """从 ChromaDB 外部数据集合中拉取最近 48 小时的候选内容。"""
    import chromadb

    chroma_path = config.get("chromadb", {}).get("path", "")
    if not chroma_path:
        print("⚠️ 未配置 ChromaDB 路径，回退到 RSS 抓取")
        return fetch_all_legacy(config)

    try:
        client = get_client()
        collection = client.get_collection("content_farm")
    except Exception as e:
        print(f"⚠️ 无法连接 ChromaDB content_farm 集合: {e}")
        print("  回退到 RSS 抓取...")
        return fetch_all_legacy(config)

    # 获取所有文档 + 元数据
    try:
        results = collection.get(include=["documents", "metadatas"])
    except Exception as e:
        print(f"⚠️ ChromaDB 查询失败: {e}")
        print("  回退到 RSS 抓取...")
        return fetch_all_legacy(config)

    if not results["ids"]:
        print("📭 ChromaDB content_farm 集合为空，回退到 RSS 抓取")
        return fetch_all_legacy(config)

    # 按 fetched_at 排序，取最近 48 小时
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(hours=48)

    items = []
    for i, meta in enumerate(results["metadatas"]):
        fetched_str = meta.get("fetched_at", "")
        try:
            fetched_at = datetime.fromisoformat(fetched_str)
            if fetched_at < cutoff:
                continue
        except (ValueError, TypeError):
            pass  # 无法解析日期则不筛选

        title = meta.get("title", "")
        source = meta.get("source", "unknown")
        url = meta.get("url", "")
        doc = results["documents"][i] if i < len(results["documents"]) else ""

        items.append({
            "title": title,
            "link": url,
            "summary": doc[:500] if doc else title,
            "source": source,
            "score": meta.get("score", meta.get("stars", 0)),
        })

    # ── 去重 ──
    history_urls = build_history_urls()
    items = dedup_items(items, history_urls)

    # ── 质量过滤：去掉水帖/合集/每周讨论 ──
    junk_patterns = [
        r"Self.Promotion.Thread", r"Weekly.Discussion", r"Moronic Monday",
        r"Simple Questions", r"megathread", r"weekly thread",
        r"\[D\]", r"AMA Series", r"monthly .*? thread",
    ]
    quality_items = []
    for it in items:
        title = it.get("title", "")
        if not any(re.search(p, title, re.IGNORECASE) for p in junk_patterns):
            quality_items.append(it)
    if len(quality_items) < len(items):
        print(f"🧹 质量过滤：{len(items)} → {len(quality_items)} 条")

    items = quality_items

    # 按热度（score/stars）降序排列
    items.sort(key=lambda x: x.get("score", 0), reverse=True)

    print(f"📊 从向量库拉取 {len(items)} 条（最近 48 小时，来自 V2EX/Reddit/GitHub 等）")

    # ── 如果时间过滤后为空，回退到 RSS 抓取 ──
    if not items:
        print("   ⚠️ 时间过滤后无内容，回退到 RSS 抓取...")
        return fetch_all_legacy(config)

    return items


def fetch_all_legacy(config: dict) -> list:
    """直接抓取模式。Reddit 合并请求 + V2EX + HN + GitHub。"""
    print("📡 直接抓取模式...\n")
    all_items = []

    # Reddit: 合并所有子版块到一个 RSS 请求（避免 429 限流）
    reddit_subs = config["sources"].get("reddit", [])
    if reddit_subs:
        print(f"  🔴 Reddit（合并 {len(reddit_subs)} 个子版块）...")
        items = fetch_reddit(reddit_subs)
        print(f"     → {len(items)} 条")
        all_items.extend(items)

    # V2EX RSS（走代理稳定）
    for url in config["sources"].get("v2ex", []):
        node = url.split("/")[-1].replace(".xml", "")
        print(f"  🟢 V2EX {node}...")
        items = fetch_rss(url)
        print(f"     → {len(items)} 条")
        all_items.extend(items)

    hn_url = config["sources"].get("hackernews", "")
    if hn_url:
        print(f"  🟠 Hacker News...")
        items = fetch_rss(hn_url)
        print(f"     → {len(items)} 条")
        all_items.extend(items)

    if config["sources"].get("github_trending"):
        print(f"  🐙 GitHub Trending...")
        items = fetch_github_trending()
        print(f"     → {len(items)} 条")
        all_items.extend(items)

    ph_url = config["sources"].get("producthunt", "")
    if ph_url:
        print(f"  🧡 Product Hunt...")
        items = fetch_rss(ph_url)
        print(f"     → {len(items)} 条")
        all_items.extend(items)

    print(f"\n📊 共抓取 {len(all_items)} 条")

    # ── 去重 ──
    history_urls = build_history_urls()
    all_items = dedup_items(all_items, history_urls)

    return all_items


def hermes_pick_and_rewrite(items: list, config: dict) -> dict:
    """用 Hermes 筛选最佳 + 改写小红书风格"""
    if not items:
        return None

    print("\n🔮 Hermes 正在筛选+改写...")

    # 构建待选列表（只取前15条，减少处理时间）
    candidates = "\n\n".join([
        f"[{i+1}] 标题: {item['title']}\n    摘要: {item['summary'][:150]}\n    来源: {item['source']}"
        for i, item in enumerate(items[:15])
    ])

    style = config["xhs_style"]

    prompt = f"""你是一个小红书AI科技博主。从以下海外热帖中选出最能在小红书引发关注的一条，改写成小红书风格。

{candidates}

只输出JSON（不要其他内容）：
{{
  "pick_index": 数字(1-15),
  "pick_reason": "为什么选这条",
  "title": "小红书标题（15-25字，带emoji）",
  "content": "正文（{style}）",
  "tags": ["标签1", "标签2", "标签3"]
}}"""

    try:
        raw = subprocess.check_output(
            ["hermes", "-z", prompt],
            timeout=300, encoding="utf-8", errors="replace"
        )
        raw = raw.strip()

        # 提取 JSON（支持 markdown 代码块包裹）
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', raw)
        if not json_match:
            json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            try:
                return json.loads(json_match.group(1) if json_match.lastindex else json_match.group())
            except json.JSONDecodeError:
                # 尝试修复常见 JSON 错误：尾逗号、裸引号
                cleaned = re.sub(r',\s*([}\]])', r'\1', json_match.group())
                return json.loads(cleaned)
        print(f"  ⚠️ 解析失败，原始输出: {raw[:300]}")
        return None
    except Exception as e:
        print(f"  ⚠️ Hermes 出错: {e}")
        return None



def save_result(result: dict, items: list):
    """保存结果到 JSON 并打印"""
    today = datetime.now().strftime("%Y%m%d")

    pick_idx = result.get("pick_index", 0)
    pick_url = ""
    if pick_idx and 1 <= pick_idx <= len(items):
        pick_url = items[pick_idx - 1].get("link", "")

    output = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "source_items_count": len(items),
        "post": result,
        "pick_url": pick_url,
    }
    json_path = OUTPUT_DIR / f"{today}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存: {json_path}")
    return str(json_path)


def generate_review_html(result: dict, json_path: str):
    """生成审核页 HTML"""
    today = datetime.now().strftime("%Y%m%d")
    post = result
    title = post.get("title", "")
    content = post.get("content", "")
    tags = post.get("tags", [])
    reason = post.get("pick_reason", "")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>审核 - {today}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f0eb;color:#333;max-width:520px;margin:0 auto;padding:0}}
.card{{background:#fff;margin:16px;border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06)}}
.card .body{{padding:16px}}
.card h2{{font-size:1.2rem;line-height:1.5;margin-bottom:8px}}
.card .reason{{font-size:.8rem;color:#999;margin-bottom:12px;padding:8px 12px;background:#f9f5ff;border-radius:8px}}
.card .content{{font-size:.9rem;line-height:1.7;white-space:pre-wrap}}
.card .tags{{margin-top:12px;display:flex;gap:6px;flex-wrap:wrap}}
.card .tags span{{background:#f0f0f0;color:#666;padding:4px 10px;border-radius:12px;font-size:.75rem}}
.actions{{padding:0 16px 30px;display:flex;gap:10px}}
.actions button{{flex:1;padding:12px;border:none;border-radius:12px;font-size:1rem;cursor:pointer;font-weight:600}}
.btn-pass{{background:#6d28d9;color:#fff}}
.btn-edit{{background:#f0f0f0;color:#333}}
.btn-delete{{background:#fef2f2;color:#dc2626}}
.editor{{display:none;margin:16px}}
.editor textarea{{width:100%;padding:12px;border:1px solid #ddd;border-radius:12px;font-size:.9rem;line-height:1.6;font-family:inherit;min-height:200px}}
.editor input{{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px;font-size:.9rem;margin-bottom:8px;font-family:inherit}}
.header{{padding:20px 16px 0;text-align:center}}
.header h1{{font-size:1.1rem;color:#999;font-weight:400}}
.header .date{{font-size:.85rem;color:#ccc}}
</style>
</head>
<body>
<div class="header">
  <h1>📋 每日内容审核</h1>
  <div class="date">{today}</div>
</div>

<div class="card" id="preview-card">
  <div class="body">
    <div class="reason">💡 选这条原因：{reason}</div>
    <h2 id="preview-title">{title}</h2>
    <div class="content" id="preview-content">{content}</div>
    <div class="tags" id="preview-tags">
      {''.join(f'<span>{t}</span>' for t in tags)}
    </div>
  </div>
</div>

<div class="actions" id="action-btns">
  <button class="btn-delete" onclick="deletePost()">🗑 删掉</button>
  <button class="btn-edit" onclick="editPost()">✏️ 修改</button>
  <button class="btn-pass" onclick="approvePost()">✅ 通过</button>
</div>

<div class="editor" id="editor">
  <input id="edit-title" placeholder="标题">
  <textarea id="edit-content" placeholder="正文"></textarea>
  <div class="actions">
    <button class="btn-delete" onclick="cancelEdit()">取消</button>
    <button class="btn-pass" onclick="saveEdit()">保存</button>
  </div>
</div>

<script>
const DATE = "{today}";

function editPost() {{
  document.getElementById('preview-card').style.display = 'none';
  document.getElementById('action-btns').style.display = 'none';
  document.getElementById('editor').style.display = 'block';
  document.getElementById('edit-title').value = document.getElementById('preview-title').textContent;
  document.getElementById('edit-content').value = document.getElementById('preview-content').textContent;
}}

function cancelEdit() {{
  document.getElementById('preview-card').style.display = 'block';
  document.getElementById('action-btns').style.display = 'flex';
  document.getElementById('editor').style.display = 'none';
}}

async function saveEdit() {{
  const title = document.getElementById('edit-title').value;
  const content = document.getElementById('edit-content').value;
  await fetch('/api/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{date: DATE, title, content, tags: []}})
  }});
  document.getElementById('preview-title').textContent = title;
  document.getElementById('preview-content').textContent = content;
  cancelEdit();
}}

async function approvePost() {{
  await fetch('/api/approve', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{date: DATE}})
  }});
  alert('✅ 已标记通过！');
}}

async function deletePost() {{
  if (!confirm('确定删除？')) return;
  await fetch('/api/delete', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{date: DATE}})
  }});
  alert('🗑 已删除');
}}
</script>
</body>
</html>"""

    html_path = OUTPUT_DIR / f"{today}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"📄 审核页已生成: {html_path}")
    return str(html_path)


def launch_review(html_path: str, json_path: str):
    """启动审核服务并在浏览器中打开"""
    import socket

    # 检查是否已在运行
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    already_running = sock.connect_ex(("127.0.0.1", 8776)) == 0
    sock.close()

    if not already_running:
        # Windows 独立后台进程，不随父进程退出
        if sys.platform == "win32":
            subprocess.Popen(
                [sys.executable, str(ROOT / "review_server.py")],
                cwd=str(ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(ROOT / "review_server.py")],
                cwd=str(ROOT),
                start_new_session=True,
            )
        time.sleep(1)

    today = datetime.now().strftime("%Y%m%d")
    url = f"http://127.0.0.1:8776/review/{today}"
    print(f"🌐 打开审核页: {url}")
    webbrowser.open(url)


def main():
    print("=" * 50)
    print("  🤖 AI 信息差搬运工 — 每日一条")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    config = load_config()
    _setup_proxy(config)

    # Step 1: 抓取新鲜内容（RSS）+ ChromaDB 历史去重
    items = fetch_all_legacy(config)

    if not items:
        print("\n❌ 没有抓到任何内容。请检查 Clash Verge 是否运行（端口 7897）。")
        sys.exit(1)

    # Step 1.5: 保存原始数据 + 入库 ChromaDB
    try:
        today = datetime.now().strftime("%Y%m%d")
        raw_path = OUTPUT_DIR / f"{today}_raw.json"
        raw_data = []
        for item in items:
            raw_data.append({
                "title": item.get("title", ""),
                "content": item.get("summary", ""),
                "url": item.get("link", ""),
                "source": item.get("source", ""),
                "source_type": "content-farm",
                "fetched_at": datetime.now().isoformat()
            })
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, indent=2)
        print(f"\n💾 原始数据已保存: {raw_path} ({len(raw_data)} 条)")

        # 增量入库 ChromaDB content_farm 集合（连接 HTTP 服务）
        import hashlib
        chroma_path = config.get("chromadb", {}).get("path", "")
        if chroma_path:
            try:
                import chromadb
                client = get_client()
                collection = client.get_or_create_collection(
                    name="content_farm", metadata={"hnsw:space": "cosine"},
                )

                # 读取已存在 URL 去重
                existing_urls = set()
                try:
                    all_data = collection.get(include=["metadatas"])
                    for meta in all_data["metadatas"]:
                        url = meta.get("url", "")
                        if url:
                            existing_urls.add(url)
                except Exception:
                    pass

                ids, docs, metas = [], [], []
                skipped = 0
                for item in raw_data:
                    url = item.get("url", "")
                    if url and url in existing_urls:
                        skipped += 1
                        continue
                    uid = hashlib.md5((url or item["title"]).encode()).hexdigest()
                    ids.append(uid)
                    docs.append(item["content"][:1000])
                    metas.append({
                        "title": item["title"],
                        "url": item["url"],
                        "source": item["source"],
                        "source_type": "content-farm",
                        "fetched_at": item["fetched_at"],
                    })
                    existing_urls.add(url)  # 防同批次重复

                if not ids:
                    print(f"📭 ChromaDB: {skipped} 条均已在库，无新增")
                else:
                    # 分批嵌入（768 维，Ollama nomic-embed-text）
                    OLLAMA_URL = "http://localhost:11434/api/embed"
                    EMBED_MODEL = config.get("chromadb", {}).get("embedding_model", "nomic-embed-text")
                    BATCH_SIZE = 50

                    # 1) 先全部嵌入
                    all_embeddings = []
                    for i in range(0, len(docs), BATCH_SIZE):
                        batch_docs = docs[i:i + BATCH_SIZE]
                        payload = json.dumps({"model": EMBED_MODEL, "input": batch_docs}).encode("utf-8")
                        req = urllib.request.Request(OLLAMA_URL, data=payload,
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            result = json.loads(resp.read().decode("utf-8"))
                        all_embeddings.extend(result["embeddings"])

                    # 2) 语义去重：检查每一条是否与库中已有内容高度相似
                    semantic_dup = 0
                    final_ids, final_docs, final_metas, final_embs = [], [], [], []
                    for idx in range(len(docs)):
                        try:
                            res = collection.query(query_embeddings=[all_embeddings[idx]], n_results=1)
                            if res["distances"] and res["distances"][0]:
                                similarity = 1 - res["distances"][0][0]
                                if similarity >= 0.95:
                                    semantic_dup += 1
                                    continue
                        except Exception:
                            pass
                        final_ids.append(ids[idx])
                        final_docs.append(docs[idx])
                        final_metas.append(metas[idx])
                        final_embs.append(all_embeddings[idx])

                    # 3) 入库非重复内容
                    added = 0
                    for i in range(0, len(final_docs), BATCH_SIZE):
                        batch_ids = final_ids[i:i + BATCH_SIZE]
                        batch_docs = final_docs[i:i + BATCH_SIZE]
                        batch_metas = final_metas[i:i + BATCH_SIZE]
                        batch_embs = final_embs[i:i + BATCH_SIZE]
                        collection.add(ids=batch_ids, embeddings=batch_embs,
                            documents=batch_docs, metadatas=batch_metas)
                        added += len(batch_docs)
                    print(f"📦 已入库 ChromaDB: {added} 条新增（URL去重跳过 {skipped} 条，语义去重跳过 {semantic_dup} 条）→ content_farm 集合")
            except Exception as e:
                print(f"  ⚠️ ChromaDB 入库失败（不阻塞）: {e}")
    except Exception as e:
        print(f"  ⚠️ 保存原始数据失败（不阻塞主流程）: {e}")

    # Step 2: Hermes 筛选 + 改写
    result = hermes_pick_and_rewrite(items, config)

    if not result:
        # 降级：取第一条有实质内容的，用简短中文包装
        print(f"\n  ⚠️ Hermes 失败，使用备选方案")
        fallback = items[0]
        # 过滤掉纯英文标题：优先选含中文或明显是产品的
        for it in items[:10]:
            t = it.get("title", "")
            # 跳过水帖（质量过滤的兜底）
            if any(kw in t.lower() for kw in ["self-promotion", "weekly", "megathread", "[d]", "simple questions"]):
                continue
            fallback = it
            break

        fb_title = fallback.get("title", "")[:80]
        fb_summary = fallback.get("summary", "") or fb_title
        fb_url = fallback.get("link", "")

        result = {
            "pick_index": items.index(fallback) + 1 if fallback in items else 1,
            "pick_reason": "备选（Hermes/Ollama 不可用，请检查 ollama serve 是否运行）",
            "title": fb_title[:30],
            "content": f"今天 AI 圈有个值得关注的内容：\n\n"
                       f"{fb_title}\n\n"
                       f"简介：{fb_summary[:300]}\n\n"
                       f"来源：{fb_url}\n\n"
                       f"💡 提示：Hermes 改写失败，内容为原文摘要，建议手动润色后发布。",
            "tags": ["AI", "科技", "资讯"]
        }

    # Step 3: 保存结果
    json_path = save_result(result, items)

    # Step 4: 生成审核页 HTML
    html_path = generate_review_html(result, json_path)

    # Step 5: 启动审核服务 + 弹浏览器
    launch_review(html_path, json_path)

    print("\n" + "=" * 50)
    print(f"  ✅ 管线完成！")
    print(f"  📄 结果: {json_path}")
    print(f"  📝 标题: {result.get('title', '')}")
    print(f"  🏷 标签: {', '.join(result.get('tags', []))}")
    print("=" * 50)


if __name__ == "__main__":
    main()
