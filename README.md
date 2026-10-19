# font-split

把单个 TTF/OTF 字体预先、机械地拆分为多个 Unicode-aware 的 WOFF2 分片，并生成可直接部署的 CSS 与 schema-2 `manifest.json`。

分片是静态且完整的，不按网页内容动态生成 subset。浏览器根据 `@font-face` 的 `unicode-range`，只下载页面字符命中的分片；每个分片只包含自己负责的码点，以及 OpenType 布局所需的依赖 glyph。

这是 [`fonts-cdn`](https://github.com/SharpDotNUT/fonts-cdn) 使用的分片引擎，也可以作为独立 CLI 或 Python 包使用。

## 核心特性

- 按字体实际 cmap 分桶，不会为不存在的码点生成范围。
- 按语言区域 + 字频/码点顺序切分，桶内使用 `i^(1/3)` 递增预算：首片小，后续逐步增大。
- 按区域真实编码体积校准 bytes/char，而不是全局平均值。
- 提取 GSUB/GPOS 同片约束，保证 kerning、ligature、mark、上下文规则不会跨分片失效。
- 可选 fallback 源：只发布 `fallback cmap - primary cmap`，用于语言子集字体缺字时回退。
- 确定性 WOFF2 输出（`recalcTimestamp=False`），内容哈希命名，可长期 immutable 缓存。
- 真实构建内置八项产物契约检查，失败即非零退出。
- 构建指纹门控复用；重复构建不会重新编码未变化的分片。

## 安装与运行

要求 Python ≥ 3.11 与 [uv](https://docs.astral.sh/uv/)。

独立使用：

```bash
git clone https://github.com/SharpDotNUT/font-split.git
cd font-split
uv sync
uv run font-sharder input.ttf -o dist --css-family "My Font"
```

作为 `fonts-cdn` 的 submodule 使用时，不需要单独安装：父仓库的 uv workspace 会同时安装 `fonts-cdn` 与 `font-split`，`uv run font-sharder` 可直接使用。

## 快速开始

```bash
uv run font-sharder input.ttf \
  -o dist \
  --family MyFont \
  --css-family "My Font"

# 语言子集字体：用完整覆盖字体补齐缺失码点
uv run font-sharder SourceHanSansJP-VF.ttf \
  -o dist \
  --css-family "Source Han Sans JP" \
  --fallback SourceHanSansSC-VF.ttf
```

输出：

```text
dist/MyFont/
├── MyFont.css
├── manifest.json
└── MyFont-000-latin.<hash>.woff2
```

在 HTML 中引 CSS 即可：

```html
<link rel="stylesheet" href="https://fonts.example.com/MyFont/MyFont.css">
```

## CLI 选项

| 参数 | 含义 |
| --- | --- |
| `source` | 输入字体：`.ttf`、`.otf`、`.woff`、`.woff2` 或 `.ttc` |
| `-o, --output DIR` | 输出根目录（默认 `dist`）；文件写入 `DIR/<family>/` |
| `--family SLUG` | 输出家族 slug（默认由 CSS family 推导） |
| `--css-family NAME` | CSS `font-family` 名称；省略时读取字体的 family name |
| `--fallback FONT` | fallback 源字体；只补充主 cmap 缺失的码点 |
| `-s, --chunk-size BYTES` | 每个分片的目标字节预算（默认 71680） |
| `-F, --features TAGS` | 保留的 OpenType feature：`all` 或逗号分隔（默认一组常用 feature） |
| `--deps {lookup,feature,none}` | 布局约束提取模式（默认 `lookup`） |
| `--include-cjk-ext` | 将 CJK 扩展区纳入 `cjk-ext` 桶 |
| `--areas PATH` | 覆盖内置语言区域表 |
| `-j, --jobs N` | 并行 subset/encode worker 数 |
| `-f, --force` | 忽略指纹复用，重建全部分片 |
| `--skip-baseline` | 不编码整字体 baseline（默认开启） |
| `--no-verify` | 跳过八项契约检查（仅调试） |
| `-n, --dry-run` | 只打印计划，不写文件 |
| `--manifest-out PATH` | dry-run 时额外写出 manifest |
| `--font-number N` | 字体集合（TTC/OTC）中的 face 序号 |
| `-v, --verbose` | 输出详细分片计划 |

退出码：`0` 成功，非零表示构建或契约检查失败。

## 输出契约

### 文件命名

分片命名为 `<Family>-<order>-<tag>.<blake2b-8hex>.woff2`：

- `order` 与 CSS `@font-face` 顺序一致，宽度至少 3 位；
- `tag` 是区域标签（如 `latin`、`han`、`kana`、`hangul`、`cjk-ext`、`misc`）；
- `hash` 是编码后内容哈希，内容不变则 URL 不变。

CSS 与 manifest 在固定路径覆盖写入，应使用短缓存；分片内容哈希不变，可长期缓存。

### CSS

每个分片对应一个 `@font-face`，`unicode-range` 精确等于该分片的规划码点。可变字体从 `fvar` 读取 `wght` 范围；静态字体使用 `OS/2.usWeightClass`。

### manifest

schema 2，包含：

- `fontFamily`、`slug`、`fingerprint`；
- `build.source` / `build.fallback`（文件名、SHA-256、字节数、码点数、分片数）；
- `build.tool`、`build.config`、`build.summary`；
- `shards[]`：`file`、`order`、`tag`、`area`、`origin`（`primary` / `fallback`）、`chars`、`bytes`、`unicodeRanges`、`blake2b`。

manifest 的分片清单同时是上传与完整性校验清单。

## 算法

1. **扫描 cmap**：读取源字体实际支持的码点。
2. **区域分桶**：按版本化语言区域表把码点分到 `latin`、`han`、`kana`、`hangul`、`cjk-ext` 等桶；无法归类的进入 `misc`。
3. **密度校准**：对每个桶抽样 subset + WOFF2 编码，得到该区域的真实 bytes/char；并测量共享闭包的比例。
4. **切分**：桶内按目标字节预算和 `i^(1/3)` 增长率决定每片字符数，首片预算约为目标的 1/5。
5. **布局约束**：逐条提取 GSUB/GPOS 规则需要的字符集合；单 substitution/positioning 自包含，ligature、pair、mark、context 等规则要求相关字符同片。超过 `MAX_GROUP_CHARS` 的大类规则被跳过，依赖 subsetter 的闭包，可能产生重复 glyph。
6. **约束合并**：把每个约束涉及的字符移动到已包含其中字符的最早分片；多轮迭代直到不再移动。
7. **subset + 编码**：按分片 origin 选择主源或 fallback 源，用 HarfBuzz subsetter 保留特性与依赖，裁剪 cmap 到规划码点，再编码为确定性 WOFF2。
8. **写产物**：生成 manifest、CSS，清理该家族目录中的过期分片，并执行八项自检。

## fallback

`--fallback` 的码点集合严格为：

```text
fallback cmap - primary cmap
```

fallback 分片排在主分片之后，使用同一 CSS family 与同一输出目录，并在 manifest/CSS 中标记 `origin = "fallback"`。同片约束按 origin 分别检查；一条规则如果跨越主源与 fallback 源，不要求共享分片，因为字符来自不同字体文件。

空差集不会生成 fallback 分片，但仍保留 fallback 源元数据。

## 指纹与复用

构建指纹包含：

- 主源 SHA-256；
- fallback 源 SHA-256（如有）；
- `font-split` 版本；
- `SHARD_PIPELINE`；
- `chunk_size`、layout features、constraints 模式、`include_cjk_ext`、area table 版本、WOFF2 质量等影响字节的配置。

指纹一致时，按分片序号复用现有文件，并校验内容哈希；否则重建。完成的重建会删除该家族目录中不在本次清单内的分片。输出字节因代码变化而改变、但没有新增用户选项时，必须递增 `SHARD_PIPELINE`。

## 自检

每次真实构建执行八项检查：

1. 源 cmap 中未被显式排除的每个码点都有分片覆盖；
2. 任意码点只出现在一个分片中；
3. `unicode-range` 能往返解析，且每个 WOFF2 的实际 cmap 与规划字符集完全一致；
4. 所有保留下来的 GSUB/GPOS 同片约束都落在单个分片内；
5. manifest 的字节数与内容哈希和磁盘文件一致；
6. 抽样分片可读、保留必需表、字符不落到 `.notdef`，且 `liga`/`vert` 行为与源字体一致；
7. CSS 的 `@font-face` 数量、引用文件名与分片清单一一对应；
8. 实际总字节数与预测值的偏差不超过 20%。

## 开发

```bash
uv sync
uv run font-sharder input.ttf -o dist --css-family "Test"
uv run font-sharder input.ttf -o dist --css-family "Test"   # 预期全部复用
uv tool run ruff check src tools
uv tool run ruff format --check src tools
```

项目没有单元测试套件；一次真实构建 + 复用构建 + ruff 是发布门槛。

## 数据来源与许可

语言区域与字频数据派生自 [`cn-font-split`](https://github.com/KonghaYao/cn-font-split)，许可见 [`NOTICE`](NOTICE)。数据已提交到仓库，正常构建不访问网络。重新生成：

```bash
uv run --group dev python tools/gen_lang_data.py
```

## 延伸阅读

- [产物契约](docs/font-artifact-contract.md)
- [Python 实现说明](docs/python-implementation.md)
- [cn-font-split 背景研究](docs/cn-font-split-research.md)
