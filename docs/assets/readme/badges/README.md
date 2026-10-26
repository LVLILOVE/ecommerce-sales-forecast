# README 徽章（本地生成，不依赖第三方服务）

这 5 枚 PNG 徽章是**在仓库内本地生成**的，刻意没有用 shields.io。

## 为什么不用 shields.io

`img.shields.io` 在中国大陆经常超时或返回占位图，README 顶部就会出现 5 个破图 —— 比没有徽章更难看。
本地图片随仓库分发，**任何网络环境下都能显示**。

## 为什么用 PNG 而不是 SVG

先说一个容易搞错的点（这里更正过一次）：**`raw.githubusercontent.com` 对 `.svg` 返回的是 `image/svg+xml`，不是 `text/plain`**，
所以本地 SVG 其实**能**在 GitHub 上作为 `<img>` 渲染。

选 PNG 的真实理由是**跨渲染器的一致性**：

| 渲染场景 | PNG | SVG |
|---|---|---|
| github.com 网页 | ✅ | ✅ |
| `camo.githubusercontent.com` 代理 | ✅ | 依赖其 sanitize 规则 |
| IDE / 编辑器内置 Markdown 预览 | ✅ | 部分不支持 |
| 导出的 HTML / PDF（本项目会把 md 转 docx·pdf） | ✅ | 需额外栅格化 |

## 当前徽章

| 文件 | 文本 | 颜色 | 含义 |
|---|---|---|---|
| `LSTM-PyTorch.png` | `LSTM · PyTorch 2.9` | `#6e40c9` | 算法层 |
| `FastAPI-SQLite.png` | `FastAPI + SQLite` | `#0e7490` | 服务与存储 |
| `React-TS.png` | `React 18 + TypeScript` | `#1f6feb` | 前端 |
| `WAPE.png` | `WAPE 14.26% (≤20%)` | `#1a7f37` | 主指标与阈值 |
| `test-suites.png` | `38 auto-check suites` | `#57606a` | 质量保障规模 |

## 重新生成（改数字后重跑）

```python
from PIL import Image, ImageDraw, ImageFont
import pathlib

OUT = pathlib.Path("docs/assets/readme/badges")
FONT = "C:/Windows/Fonts/segoeui.ttf"
S, H, FS, PAD, R = 4, 30, 13, 11, 7      # 超采样倍数 / 高 / 字号 / 左右内边距 / 圆角

BADGES = [
    ("LSTM-PyTorch",   "LSTM · PyTorch 2.9",   "#6e40c9"),
    ("FastAPI-SQLite", "FastAPI + SQLite",      "#0e7490"),
    ("React-TS",       "React 18 + TypeScript", "#1f6feb"),
    ("WAPE",           "WAPE 14.26% (≤20%)",    "#1a7f37"),
    ("test-suites",    "38 auto-check suites",  "#57606a"),
]
font = ImageFont.truetype(FONT, FS * S)
probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

for slug, text, color in BADGES:
    bbox = probe.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    W = tw // S + PAD * 2
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W * S - 1, H * S - 1], radius=R * S, fill=color)
    asc, desc = font.getmetrics()
    d.text(((W * S - tw) // 2 - bbox[0], (H * S - (asc + desc)) // 2),
           text, font=font, fill="#ffffff")
    img.resize((W, H), Image.LANCZOS).save(OUT / f"{slug}.png", optimize=True)
```

要点：

- **4 倍超采样再降采样**（`S=4` + `LANCZOS`）：直接按 13px 画会有锯齿，这是让文字看起来像专业徽章的关键。
- **垂直居中用 `font.getmetrics()`**：`(ascent + descent)` 比 `textbbox` 的 height 更稳，中英文混排不会偏基线。
- **宽度按文本实测**：不写死，改文案后自动适配（当前 5 枚合计 727 px，GitHub 正文宽约 880 px，一行放得下）。

依赖 Pillow（本机在 `C:\Users\Administrator\.workbuddy\binaries\python\envs\default`，`docx` 环境里没有）。
