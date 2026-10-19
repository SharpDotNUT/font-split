# 产物契约：预分片字体 CDN 构建输出

本文定义 `font-sharder` 的输出：目录布局、命名、manifest schema、CSS、算法与 CDN 缓存约定。

## 1. 命名与缓存

分片文件名为 `<Family>-<序号>-<语义标签>.<blake2b-8hex>.woff2`。序号对应 CSS face 顺序；语义标签标识字符区域；内容哈希使每个分片 URL 可长期 immutable 缓存。

所有输出位于稳定路径 `dist/<Family>/`，没有版本目录。CSS 和 manifest 在重建时覆盖，必须使用短缓存；哈希变化会生成新分片名。

## 2. 目录结构

```text
dist/SourceHanSansCN/
├── SourceHanSansCN.css
├── manifest.json                               # schema 2，含 build.fallback
├── SourceHanSansCN-000-latin.<hash>.woff2
└── …
```

序号宽度为 `max(3, len(str(shard_count)))`。顺序由 area table 决定；每片字符集互斥。

## 3. 语义标签

`latin`、`latin1`、`latinext`、`ipa`、`greek`、`cyrillic`、`sym`、`han`、`kana`、`jamo`、`hangul`、`full`、`cjk-ext`、`misc` 表示各 area 的字符集。area table 是随项目发布的版本化数据。

## 4. `manifest.json`

Schema 2。CSS 是 manifest 中 shards 顺序和字符范围的派生物。`build.fallback` 始终存在：无 fallback 为 `null`；有 fallback 时记录源文件、SHA-256、字节数、补充码点数和 shard 数。每条 shard 记录 `origin` (`primary` / `fallback`)。

```json
{
  "schema": 2,
  "fontFamily": "Source Han Sans CN",
  "slug": "SourceHanSansCN",
  "fingerprint": "…",
  "build": {
    "source": {"file": "SourceHanSansCN-VF.ttf", "sha256": "…", "bytes": 1},
    "fallback": {
      "source": {"file": "SourceHanSansSC-VF.ttf", "sha256": "…", "bytes": 1},
      "codepoints": 100,
      "shards": 2
    },
    "tool": {"name": "font-sharder", "version": "0.1.0"},
    "config": {},
    "summary": {}
  },
  "shards": [{
    "file": "SourceHanSansCN-000-latin.<hash>.woff2",
    "order": 0,
    "tag": "latin",
    "area": "LATIN",
    "origin": "primary",
    "chars": 57,
    "bytes": 1234,
    "unicodeRanges": "U+0020-0058",
    "blake2b": "…"
  }]
}
```

`shards[].order` matches filename order and CSS order. `unicodeRanges` is the sorted, compact union of exactly the shard codepoints; shards never overlap. `bytes` and `blake2b` describe actual output files. `fingerprint` includes primary and fallback source hashes and byte-affecting configuration.

## 5. Algorithm and fallback

Primary input is bucketed and planned as usual. With `--fallback`, compute `fallback cmap - primary cmap`, bucket and plan that set independently, then append its shards after primary shards. Fallback shards use the same CSS family and output directory. Empty difference produces no fallback shards while retaining fallback source metadata. Each shard is subset with its origin's source font. Co-location constraints are enforced per origin: a fallback rule is kept only for codepoints in that difference.

## 6. CSS

One `<Family>.css` per family, faces in shard order. Each face uses `unicode-range` for exactly its planned characters and the font's variable `wght` range when present; static faces use their `OS/2` weight.

## 7. Deployment

Object keys have the form `SourceHanSansCN/SourceHanSansCN-000-latin.<hash>.woff2`. Serve WOFF2 with `Content-Type: font/woff2` and `Cache-Control: public, max-age=31536000, immutable`. Serve CSS and manifest with `Cache-Control: public, max-age=300`. Configure CORS for browser font requests. The manifest shard list is the upload and integrity checklist.

## 8. 构建自检与报告

每次真实构建（`verify` 未关闭）在写产物后执行八项检查：

1. 源 cmap 中未被显式排除的每个码点都有分片覆盖；
2. 任意码点只出现在一个分片中；
3. `unicode-range` 能往返解析，且每个 WOFF2 的实际 cmap 与规划字符集完全一致；
4. 所有保留下来的 GSUB/GPOS 同片约束都落在一个分片内（按 origin 分别检查）；
5. manifest 中每个分片的字节数与 `blake2b` 和磁盘文件一致；
6. 抽样分片可读、保留必需表、字符不落到 `.notdef`，且 `liga`/`vert` 行为与源字体一致；
7. CSS 的 `@font-face` 数量、引用文件名与分片清单一一对应；
8. 实际总字节数与预测值的偏差不超过 20%。

任一检查失败时该家族标记为 `failed`，错误写入 `<output_dir>/build-report.md`，命令以非零码退出；已写出的分片不会自动回滚，修复后应重建该家族。

复用同样受契约约束：只有 `fingerprint`（源文件、fallback 源、影响字节的配置、`SHARD_PIPELINE`）一致时才按分片序号复用已有文件；一次完成的重建会删除该家族目录中不在本次清单内的旧分片。
