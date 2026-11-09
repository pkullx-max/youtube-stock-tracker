"""Render the daily Markdown report as a readable, self-contained HTML page."""

import argparse
import html
import re
from pathlib import Path


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: f'<a href="{match.group(2).removeprefix("&lt;").removesuffix("&gt;")}">{match.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    output = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:1160px;margin:24px auto;padding:0 18px;color:#222;line-height:1.65}"
        "h1{line-height:1.3}h2{margin-top:38px;border-top:1px solid #ddd;padding-top:22px}"
        "h3{margin-top:28px}a{color:#075eb4}table{border-collapse:collapse;width:100%;"
        "margin:18px 0;display:block;overflow-x:auto}th,td{border:1px solid #d6dce2;"
        "padding:9px;text-align:left;vertical-align:top;min-width:110px}"
        "th{background:#edf2f7}tr:nth-child(even){background:#f9fbfc}"
        "aside{background:#f4f7fa;border-left:4px solid #7c9ec1;padding:10px 14px}"
        "li{margin:8px 0}code{background:#f1f3f5;padding:1px 4px}</style></head><body>",
    ]
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and re.fullmatch(r"[\s|:-]+", lines[index + 1]):
            output.append("<table><thead><tr>" + "".join(f"<th>{inline(cell)}</th>" for cell in cells(line)) + "</tr></thead><tbody>")
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                output.append("<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in cells(lines[index])) + "</tr>")
                index += 1
            output.append("</tbody></table>")
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if 1 <= level <= 6 and line[level:level + 1] == " ":
                output.append(f"<h{level}>{inline(line[level + 1:])}</h{level}>")
                index += 1
                continue
        if line.startswith("> "):
            output.append(f"<aside>{inline(line[2:])}</aside>")
            index += 1
            continue
        if line.startswith("- ") or re.match(r"\d+\. ", line):
            ordered = not line.startswith("- ")
            tag = "ol" if ordered else "ul"
            output.append(f"<{tag}>")
            while index < len(lines):
                item = lines[index].strip()
                if ordered:
                    item = re.sub(r"^\d+\.\s+", "", item)
                    if item == lines[index].strip():
                        break
                elif item.startswith("- "):
                    item = item[2:]
                else:
                    break
                output.append(f"<li>{inline(item)}</li>")
                index += 1
            output.append(f"</{tag}>")
            continue
        output.append(f"<p>{inline(line)}</p>")
        index += 1
    output.append("</body></html>")
    return "\n".join(output) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    args.target.write_text(render(args.source.read_text(), args.source.stem), encoding="utf-8")


if __name__ == "__main__":
    main()
