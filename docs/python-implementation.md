# Python 实现说明

`font-split` 要求 Python 3.11+，通过 `uharfbuzz` 使用 HarfBuzz，通过 fontTools 读取 OpenType 并编码 WOFF2，另用 Brotli 压缩。依赖以 wheel 形式发布，不需要本地编译器。

## 安装与运行

```bash
uv sync
uv run font-sharder input.ttf --output dist --family MyFont
uv run font-sharder input.ttf --output dist --family MyFont --fallback full-coverage.ttf
```

`font-sharder` 是单源 CLI。`--fallback` 会把主源缺失的码点放入同家族后续分片。作为 `fonts-cdn` 的 submodule 使用时，父仓库的 uv workspace 负责安装本包，`fonts-build` 直接调用同一套函数。

## 模块职责

```text
src/font_split/
├── build.py         # 端到端分片构建与 fallback 规划
├── cli.py           # font-sharder CLI
├── config.py        # BuildConfig 与产物指纹
├── scanner.py       # cmap 扫描与区域分桶
├── areas.py         # 版本化语言区域表
├── density.py       # 按区域的字节校准与闭包折扣
├── planner.py       # `i^(1/3)` 切分与约束合并
├── layout_deps.py   # GSUB/GPOS 同片约束提取
├── subsetter.py     # 每线程 HarfBuzz subsetter
├── encoder.py       # 确定性 WOFF2 编码与哈希
├── unicode_range.py # unicode-range 合并/格式化辅助
├── manifest.py      # schema 2 manifest 构造
├── errors.py        # FontSplitError / ConfigError
├── util.py          # 字节格式化
└── verify.py        # 八项产物契约检查
```

fallback 源摘要参与构建指纹，每个生成的分片都记录 `origin`。输出路径为 `dist/<Family>/`；哈希分片名支持长期 immutable 缓存，CSS 与 manifest 使用短缓存 TTL。

## 校验

每次真实构建执行八项契约检查，并在 CLI 输出中报告结果：

1. 源 cmap 中未被显式排除的每个码点都有分片覆盖；
2. 任意码点不会出现两次；
3. `unicode-range` 能往返解析，且每个编码后字体文件的 cmap 与它完全一致；
4. 所有保留下来的 GSUB/GPOS 同片约束都落在单个分片内；
5. manifest 中的字节数与内容哈希和磁盘文件一致；
6. 抽样分片可读、保留必需表、字符能正常 shape，并保留 `liga`/`vert` 行为；
7. CSS 对每个分片恰好引用一次；
8. 实际字节数与预测值的偏差不超过 20%。

用一个真实字体验证，重复运行以确认复用受指纹控制：

```bash
uv run font-sharder fonts/Test.ttf -o dist --family Test
uv run font-sharder fonts/Test.ttf -o dist --family Test   # 预期全部分片复用
uv tool run ruff check src tools
uv tool run ruff format --check src tools
```

项目没有单元测试套件；一次成功的真实构建加 ruff 通过即可作为发布门槛。`--no-verify` 会跳过检查，仅用于调试。
