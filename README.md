# AI Content Farm

> Scrape trending posts from overseas tech communities, rewrite them in Xiaohongshu (小红书) style with a local LLM, generate a cover image, and hand off to a human review page — one run per day.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/buxiangcifan123/ai-content-farm/pulls)
[![GitHub stars](https://img.shields.io/github/stars/buxiangcifan123/ai-content-farm?style=social)](https://github.com/buxiangcifan123/ai-content-farm)

## Screenshots

> Add a screenshot of the review page here — that's the most persuasive part of this project.
<!--
![review page](docs/review.png)
![pipeline](docs/demo.gif)
-->

## What it does

1. **Scrape hot posts** — pull from Reddit, V2EX, Hacker News, GitHub Trending and Product Hunt over RSS/API, plus candidates from the last 48h in the local vector store.
2. **Rewrite in 小红书 style** — Hermes CLI picks the best candidate and rewrites it into a title + body + hashtags.
3. **Cover image** — generate a cover using the configured image model.
4. **Index** — embed scraped and rewritten content into the vector store (ChromaDB / FAISS) for URL de-duplication and retrieval.
5. **Review page** — render an HTML review page at `http://127.0.0.1:8776/review/<date>` with 通过 / 修改 / 删除 (approve / edit / delete) actions.

## Requirements

- **Ollama** — local LLM / embedding service:
  - rewrite + embedding models: `gemma4:12b`, `nomic-embed-text`
- **Hermes CLI** — candidate selection and 小红书-style rewriting (`hermes -z "<prompt>"`).
- **Proxy** — scraping overseas sites needs an HTTP proxy (default `http://127.0.0.1:7897`, configurable in `config.json`).
- **Vector store** — ChromaDB / FAISS + SQLite.

## Quick start

```bash
pip install -r requirements.txt

ollama serve
ollama pull gemma4:12b
ollama pull nomic-embed-text

python pipeline.py           # run one daily generation
python review_server.py      # or serve the review page standalone (port 8776)
```

Make sure Clash / your proxy is running on `127.0.0.1:7897` and Hermes CLI is on PATH.

## Configuration (`config.json`)

| Field | Description |
|------|-------------|
| `proxy` | HTTP proxy, default `http://127.0.0.1:7897` |
| `sources` | Sources: Reddit subs, V2EX, Hacker News, GitHub Trending, Product Hunt |
| `xhs_style` | 小红书 copywriting style prompt |
| `ollama_model` | Ollama generation model, default `gemma4:12b` |
| `agnesia_model` | Image generation model, default `agnes-image-2.1-flash` |
| `chromadb.path` | Vector store path (point it at your own path) |
| `chromadb.embedding_model` | Embedding model, default `nomic-embed-text` |

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `FAISS_INDEX_DIR` | Vector index / DB directory (must be a pure-ASCII path) | `~/.ai-faiss-index` |

On Windows you can double-click `每日内容.bat`, or register `ai-content-farm-startup.vbs` for hidden-window auto-start on boot.

## License

MIT — see [LICENSE](LICENSE).

Copyright (c) 2026 buxiangcifan123

---

# AI 信息差搬运工

> 抓海外热帖 → 改写小红书风格 → 配图 → 人工审核

每天从海外技术社区抓取热帖，挑选最有传播潜力的一条，用大模型改写成小红书风格的内容，生成配图与审核页，提供一键「通过 / 修改 / 删除」的人工审核流程。

## 功能概览

1. **抓取海外热帖** — 通过 RSS / API 抓取 Reddit、V2EX、Hacker News、GitHub Trending、Product Hunt，并支持从本地向量库中拉取最近 48 小时的候选内容。
2. **改写小红书风格** — 交由 Hermes CLI 从候选中挑选最佳一条，并按小红书文案风格改写成标题 + 正文 + 话题标签。
3. **配图** — 依据配置的 Agnesia 生图模型为内容配图。
4. **入库** — 抓取结果与改写结果嵌入向量库（ChromaDB / FAISS），用于历史 URL 去重与后续检索。
5. **审核页** — 生成审核 HTML 并在浏览器中打开（`http://127.0.0.1:8776/review/<日期>`），支持通过 / 修改 / 删除。

## 依赖

- **Ollama** — 本地大模型 / 嵌入服务，需拉取 `gemma4:12b`、`nomic-embed-text`
- **Hermes CLI** — 用于候选筛选与小红书文案改写（命令 `hermes -z "<prompt>"`）
- **代理** — 抓取海外站点需要 HTTP 代理（默认 `http://127.0.0.1:7897`，可在 `config.json` 中修改）
- **向量库** — ChromaDB / FAISS + SQLite

## 安装

```bash
pip install -r requirements.txt
```

请先启动本机服务：

```bash
ollama serve            # 嵌入式 / 生成式模型服务
ollama pull gemma4:12b
ollama pull nomic-embed-text
```

并确保 Clash / 代理在 `127.0.0.1:7897` 运行、Hermes CLI 已安装并在 PATH 中。

## 配置

编辑根目录的 `config.json`（字段说明见上方英文部分 Configuration）。

| 字段 | 说明 |
|------|------|
| `proxy` | HTTP 代理地址，默认 `http://127.0.0.1:7897` |
| `sources` | 抓取源：Reddit 子版块、V2EX、Hacker News、GitHub Trending、Product Hunt |
| `xhs_style` | 小红书文案风格提示词 |
| `ollama_model` | Ollama 生成模型，默认 `gemma4:12b` |
| `agnesia_model` | Agnesia 生图模型，默认 `agnes-image-2.1-flash` |
| `chromadb.path` | 向量库路径（按需改成你自己的路径） |
| `chromadb.embedding_model` | 嵌入模型，默认 `nomic-embed-text` |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FAISS_INDEX_DIR` | 向量索引/数据库目录（须为纯 ASCII 路径） | `~/.ai-faiss-index` |

## 用法

```bash
python pipeline.py            # 执行一次每日内容生成
python review_server.py      # 单独启动审核服务（端口 8776）
python reimport_history.py   # 把历史 *_raw.json / *_external.json 重新入库
```

Windows 下可直接双击 `每日内容.bat`，或由 `ai-content-farm-startup.vbs` 注册为开机自启（隐藏窗口运行）。

## 许可

本项目基于 MIT 许可证开源，详见 [LICENSE](LICENSE)。

Copyright (c) 2026 buxiangcifan123
