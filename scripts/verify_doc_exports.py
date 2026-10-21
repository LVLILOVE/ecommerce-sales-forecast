# -*- coding: utf-8 -*-
"""校验 `docs/*.md` 与 `outputs/` 下 Word / PDF 导出件是否齐备且**未过期**。

用法：
    python scripts/verify_doc_exports.py            # 缺 pymupdf 时降级并**在结论里标明未检目录域**
    python scripts/verify_doc_exports.py --strict   # 拒绝降级：缺 pymupdf 即判未通过（交付前 / CI 用）

背景（本脚本的由来）：
    `docs/` 放 Markdown 源，`outputs/` 放交给评审的 `.docx` / `.pdf`。
    这层「源 → 导出件」的关系**没有任何机制守着** —— 改了 md 忘了重跑导出，
    评审拿到的就是旧版；而两边长得几乎一样，肉眼比对根本发现不了。
    `EP-10`（`WS-10-08` 文档收敛）把这条链固化为断言。

做四件事：
    1) 齐备性：清单里每个文档的 `.docx` / `.pdf` 都存在且大小合理；
    2) 新鲜度：导出件的 `mtime` 不得早于对应 `.md`（早于即判定「md 改了没重导」）；
    3) 目录域：PDF 首部不得残留 TOC 未展开的占位提示语（`to_pdf.ps1` 漏调 `Fields.Update` 会留下）；
    4) 页数记录：打印每份 PDF 页数，便于人工核对是否被截断。

退出码 0 表示齐备、新鲜、目录已展开。语义与排版仍需人工目视（本脚本只管这三层机械事实）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = ROOT / "outputs"

# (md 文件名, [导出文件名（不含扩展名）, ...], 是否要求新鲜)
# - 一个 md 可对应多个导出件（开发方案文档的 V1.0 / V1.1 同源）；
# - `require_fresh=False` 用于**历史快照**：它不是从当前 md 重新生成的，只查存在性。
PLAN: list[tuple[str, list[str], bool]] = [
    ("电商销量预测系统_PRD_V1.0.md",
     ["电商销量预测系统_PRD_V1.0"], True),
    ("电商销量预测系统_UIUX设计文档_V1.0.md",
     ["电商销量预测系统_UIUX设计文档_V1.0"], True),
    ("电商销量预测系统_开发技术文档_V1.0.md",
     ["电商销量预测系统_开发技术文档_V1.0"], True),
    ("电商销量预测系统_数据库设计文档_V1.0.md",
     ["电商销量预测系统_数据库设计文档_V1.0"], True),
    ("电商销量预测系统_测试报告_V1.0.md",
     ["电商销量预测系统_测试报告_V1.0"], True),
    ("电商销量预测系统_项目开发实施方案_V1.0.md",
     ["电商销量预测系统_项目开发实施方案_V1.0"], True),
    ("电商销量预测系统_部署运维手册_V1.0.md",
     ["电商销量预测系统_部署运维手册_V1.0"], True),
    ("电商销量预测系统_AI协作教程_V1.0.md",
     ["电商销量预测系统_AI协作教程_V1.0"], True),
    ("电商销量预测系统_RAG语料库与提示词_V1.0.md",
     ["电商销量预测系统_RAG语料库与提示词_V1.0"], True),
    ("电商销量预测系统_项目说明_V1.0.md",
     ["电商销量预测系统_项目说明_V1.0"], True),
    ("电商销量预测系统_演示脚本_V1.0.md",
     ["电商销量预测系统_演示脚本_V1.0"], True),
    ("电商销量预测系统_项目文件总览与交付清单_V1.0.md",
     ["电商销量预测系统_项目文件总览与交付清单_V1.0"], True),
    # 历史快照：md 名与内容版本不一致（内容已是 V1.1），只查存在性
    ("电商销量预测系统_开发方案文档_V1.0.md",
     ["电商销量预测系统_开发方案文档_V1.0", "电商销量预测系统_开发方案文档_V1.1"], False),
]

# TOC 未展开时 `build_md_docx_formal.py` 写入的占位提示语
TOC_HINT = "【请在 Word 中按 Ctrl+A 后按 F9 更新目录】"
MIN_DOCX = 8 * 1024          # 8 KB
MIN_PDF = 100 * 1024         # 100 KB


def main() -> int:
    # `--strict`：拒绝降级。缺 pymupdf 时直接判未通过（交付前/CI 用），
    # 避免「跳过检查却打印通过」这种**自证清白**的结论。
    strict = "--strict" in sys.argv[1:]
    try:
        import pymupdf
        have_pdf_lib = True
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
            have_pdf_lib = True
        except ImportError:
            have_pdf_lib = False

    print("=" * 78)
    print("文档导出件校验（齐备性 / 新鲜度 / 目录域）")
    print("=" * 78)

    fails: list[str] = []
    n_pair = 0
    toc_checked = 0

    for md_name, stems, require_fresh in PLAN:
        md = DOCS / md_name
        if not md.exists():
            fails.append(f"源 md 缺失：{md_name}")
            print(f"  [FAIL] 源 md 缺失 {md_name}")
            continue
        md_mtime = md.stat().st_mtime

        for stem in stems:
            n_pair += 1
            docx = OUT / f"{stem}.docx"
            pdf = OUT / f"{stem}.pdf"
            notes: list[str] = []
            bad = False

            for f, floor, kind in ((docx, MIN_DOCX, "docx"), (pdf, MIN_PDF, "pdf")):
                if not f.exists():
                    bad = True
                    notes.append(f"{kind} 缺失")
                elif f.stat().st_size < floor:
                    bad = True
                    notes.append(f"{kind} 过小({f.stat().st_size}B)")

            if not bad and require_fresh:
                for f, kind in ((docx, "docx"), (pdf, "pdf")):
                    if f.stat().st_mtime < md_mtime:
                        bad = True
                        notes.append(f"{kind} 早于 md（md 改了没重导）")

            pages = "-"
            if not bad and have_pdf_lib and pdf.exists():
                try:
                    d = pymupdf.open(str(pdf))
                    pages = str(d.page_count)
                    head = "".join(d[i].get_text() for i in range(min(6, d.page_count)))
                    if TOC_HINT in head:
                        bad = True
                        notes.append("TOC 域未展开")
                    d.close()
                    toc_checked += 1
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"PDF 读取失败({exc})")

            state = "FAIL" if bad else "OK"
            if bad:
                fails.append(f"{stem}: {'; '.join(notes)}")
            print(f"  [{state}] {stem}  docx={docx.stat().st_size if docx.exists() else 0}B"
                  f" pdf={pdf.stat().st_size if pdf.exists() else 0}B 页数={pages}"
                  + (f"  ← {'; '.join(notes)}" if notes else ""))

    print()
    print("-" * 78)
    print(f"受检导出件 {n_pair} 组（docx + pdf）；目录域抽查 {toc_checked} 份 PDF")
    degraded = not have_pdf_lib
    if degraded:
        print("  [WARN] 未安装 pymupdf，已跳过页数与目录域检查（仅查存在性 / 新鲜度）")
        print("         ⇒ 本次运行**不能**证明 TOC 已展开；请用装了 pymupdf 的环境复跑，或加 --strict 让本脚本拒绝降级。")
    print("-" * 78)
    if fails:
        print(f"结论：{len(fails)} 项未通过")
        for f in fails:
            print("  [FAIL]", f)
    elif degraded:
        # 🔴 降级运行时**不得**声称「目录已展开」—— 本轮就踩过：
        #    默认环境缺 pymupdf，脚本跳过了检查，结论却照打印「目录已展开」，
        #    等于给出了自己没验证过的断言。措辞必须与检查范围严格对应。
        print(f"结论：{n_pair}/{n_pair} 通过（齐备 · 新鲜）"
              f" ⚠️ 未检页数与目录域（缺 pymupdf）")
    else:
        print(f"结论：{n_pair}/{n_pair} 全部通过（齐备 · 新鲜 · 目录已展开）")
    print("=" * 78)
    if fails:
        return 1
    if degraded and strict:
        print("  [STRICT] 已要求真检，但缺少 pymupdf —— 判定为未通过")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
