"""
将 output/ 下所有已有 MD 中的截图引用（./xxx.png）替换为 base64 内嵌。
转换后 MD 文件自包含，复制到任意电脑都能正常显示截图。

用法：
    python convert_screenshots_to_base64.py
    python convert_screenshots_to_base64.py --user 祈祈_44-41-80-77   # 只处理指定用户
    python convert_screenshots_to_base64.py --dry-run                   # 预览不写盘
"""
import sys, io, os, re, base64
from pathlib import Path
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 匹配截图引用: ![问题与回答截图](./1234567890.png)
SCREENSHOT_PATTERN = re.compile(r'!\[问题与回答截图\]\(\./(\d+)\.png\)')


def convert_md_file(md_path: Path) -> tuple:
    """转换单个 MD 文件，返回 (changed, embedded_count, skipped_count)"""
    content = md_path.read_text(encoding='utf-8')
    matches = SCREENSHOT_PATTERN.findall(content)

    if not matches:
        return (False, 0, 0)

    embedded = 0
    skipped = 0

    def replacer(m):
        nonlocal embedded, skipped
        aid = m.group(1)
        png_path = md_path.parent / f"{aid}.png"
        if png_path.exists():
            b64 = base64.b64encode(png_path.read_bytes()).decode('ascii')
            embedded += 1
            return f'![问题与回答截图](data:image/png;base64,{b64})'
        else:
            skipped += 1
            return m.group(0)  # PNG 不存在，保持原样

    new_content = SCREENSHOT_PATTERN.sub(replacer, content)

    if embedded > 0:
        md_path.write_text(new_content, encoding='utf-8')
        return (True, embedded, skipped)

    return (False, embedded, skipped)


def main():
    parser = argparse.ArgumentParser(description='将 MD 中的 PNG 截图引用转为 base64 内嵌')
    parser.add_argument('--user', type=str, help='只处理指定用户目录（如 祈祈_44-41-80-77）')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不实际修改文件')
    args = parser.parse_args()

    output_root = Path(__file__).parent / 'output'
    if not output_root.exists():
        print("❌ output/ 目录不存在")
        return

    # 收集需要处理的用户目录
    if args.user:
        user_dirs = [output_root / args.user]
    else:
        user_dirs = sorted(d for d in output_root.iterdir() if d.is_dir())

    total_md = 0
    total_converted = 0
    total_embedded = 0
    total_skipped = 0

    for user_dir in user_dirs:
        if not user_dir.exists():
            print(f"⚠ 跳过不存在: {user_dir.name}")
            continue

        md_files = sorted(user_dir.glob('*.md'))
        if not md_files:
            continue

        user_converted = 0
        user_embedded = 0

        for md_path in md_files:
            total_md += 1
            md_path_bak = None

            try:
                content_before = md_path.read_text(encoding='utf-8')
                # 检查截图是否已经是 base64（精确匹配截图行，避免文中其他base64图片干扰）
                if not SCREENSHOT_PATTERN.search(content_before):
                    # 没有相对路径引用 → 已转换过或无需转换
                    continue

                changed, emb, skp = convert_md_file(md_path)
                if changed:
                    user_converted += 1
                    user_embedded += emb
                    total_skipped += skp
                    if args.dry_run:
                        # 恢复原内容
                        md_path.write_text(content_before, encoding='utf-8')
            except Exception as e:
                print(f"  ❌ {md_path.name}: {e}")

        if user_converted > 0:
            tag = "[预览] " if args.dry_run else ""
            print(f"{tag}{user_dir.name}: {user_converted}/{len(md_files)} 个MD已转换, 嵌入{user_embedded}张截图")
        total_converted += user_converted
        total_embedded += user_embedded

    tag = "[预览] " if args.dry_run else ""
    print(f"\n{tag}总计: {total_md} 个MD, {total_converted} 个转换, 嵌入{total_embedded}张截图, 缺失{total_skipped}张PNG")


if __name__ == '__main__':
    main()
