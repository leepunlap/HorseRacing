#!/usr/bin/env python3
"""Convert a Markdown whitepaper to a styled HTML, then to PDF via wkhtmltopdf."""
import sys
import subprocess
from pathlib import Path
import markdown

CSS = """
@page { size: A4; margin: 22mm 18mm 22mm 18mm; }
html, body {
  font-family: "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Noto Sans CJK SC", "DejaVu Sans", sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #1a1a1a;
  margin: 0;
  padding: 0;
}
h1 {
  font-size: 24pt;
  border-bottom: 3px solid #222;
  padding-bottom: 6pt;
  margin-top: 0.6em;
  margin-bottom: 0.4em;
  page-break-after: avoid;
}
h2 {
  font-size: 16pt;
  border-bottom: 1px solid #888;
  padding-bottom: 3pt;
  margin-top: 1.4em;
  margin-bottom: 0.5em;
  page-break-after: avoid;
}
h3 {
  font-size: 13pt;
  margin-top: 1.1em;
  margin-bottom: 0.4em;
  page-break-after: avoid;
}
h4 {
  font-size: 11.5pt;
  margin-top: 0.9em;
  margin-bottom: 0.3em;
  page-break-after: avoid;
}
p { margin: 0.4em 0 0.6em 0; text-align: justify; }
ul, ol { margin: 0.3em 0 0.6em 1.3em; padding: 0; }
li { margin: 0.15em 0; }
code {
  font-family: "DejaVu Sans Mono", monospace;
  background: #f1f1f1;
  padding: 0 3px;
  border-radius: 2px;
  font-size: 9.5pt;
}
pre code { display: block; padding: 6pt; background: #f6f6f6; border: 1px solid #ddd; }
hr {
  border: none;
  border-top: 1px solid #bbb;
  margin: 1.4em 0;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.6em 0 1em 0;
  font-size: 9pt;
  page-break-inside: avoid;
}
th, td {
  border: 1px solid #888;
  padding: 4pt 6pt;
  vertical-align: top;
  text-align: left;
}
th {
  background: #e8e8e8;
  font-weight: 600;
}
tr:nth-child(even) td { background: #fafafa; }
a { color: #1a3a8a; text-decoration: none; word-break: break-all; }
blockquote {
  margin: 0.6em 0;
  padding: 0.3em 0.8em;
  border-left: 3px solid #888;
  background: #f5f5f5;
  color: #333;
}
.toc-block { font-size: 10pt; }
@media print {
  h1, h2 { page-break-after: avoid; }
  table, pre { page-break-inside: avoid; }
}
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def md_to_html(md_text: str) -> str:
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )


def build(md_path: str, pdf_path: str, lang: str, title: str) -> None:
    md_text = Path(md_path).read_text(encoding="utf-8")
    body = md_to_html(md_text)
    html = HTML_TEMPLATE.format(lang=lang, title=title, css=CSS, body=body)
    html_path = Path(pdf_path).with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")

    cmd = [
        "wkhtmltopdf",
        "--encoding", "UTF-8",
        "--enable-local-file-access",
        "--print-media-type",
        "--page-size", "A4",
        "--margin-top", "18mm",
        "--margin-bottom", "18mm",
        "--margin-left", "16mm",
        "--margin-right", "16mm",
        "--footer-center", "[page] / [topage]",
        "--footer-font-size", "8",
        "--footer-spacing", "5",
        "--enable-smart-shrinking",
        "--quiet",
        str(html_path), pdf_path,
    ]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: build_pdf.py <md_in> <pdf_out> <lang> <title>", file=sys.stderr)
        sys.exit(2)
    build(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"wrote {sys.argv[2]}")
