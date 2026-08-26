#!/usr/bin/env python3
"""Render a design brief to a readable page. No dependencies on this box.

    ./md2html.py <in.md> <out.html> "<title>" [--img SRC|CAPTION ...]

Images ride at the top, before the spec: a reader forms a mental picture first
and reads dimensions against it. Captions carry provenance — a model's drawing
and a render off the real mesh are different kinds of evidence and must not
look alike on the page.

Covers exactly what a text2cad brief uses: headings, pipe tables, lists,
blockquotes, fenced code, bold/italic/inline-code, rules. Tables carry the
dimensions, so they get the most care — right-aligned numerics, own scroll.
"""
import html
import re
import sys
from pathlib import Path

INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"<em>\1</em>"),
]


def inline(t):
    t = html.escape(t)
    for rx, rep in INLINE:
        t = rx.sub(rep, t)
    return t


def render(md):
    out, i, lines = [], 0, md.splitlines()
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].startswith("```"):
                buf.append(lines[j]); j += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            i = j + 1; continue
        if re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) \
           and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            j = i + 2; rows = []
            while j < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[j]):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            t = ["<div class='tw'><table><thead><tr>"]
            t += [f"<th>{inline(h)}</th>" for h in head]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(
                    f"<td class='{'num' if re.match(r'^[0-9Ø×.,x  -]+$', c) else ''}'>{inline(c)}</td>"
                    for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t)); i = j; continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>"); i += 1; continue
        if re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>"); continue
        if re.match(r"^\s*\d+\.\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>"); continue
        if ln.startswith(">"):
            out.append("<blockquote>" + inline(ln.lstrip("> ")) + "</blockquote>"); i += 1; continue
        if re.match(r"^\s*---+\s*$", ln):
            out.append("<hr>"); i += 1; continue
        if ln.strip():
            buf = []
            while i < len(lines) and lines[i].strip() and not re.match(
                    r"^(#{1,4}\s|\s*[-*]\s|\s*\d+\.\s|>|```|\s*\|)", lines[i]):
                buf.append(lines[i]); i += 1
            out.append("<p>" + inline(" ".join(buf)) + "</p>"); continue
        i += 1
    return "\n".join(out)


CSS = """
:root{--paper:#fbfaf8;--surface:#fff;--ink:#1d1f22;--muted:#6d7176;--rule:#e3ded7;
--code:#f1eee9;--accent:#8a5a2b}
@media(prefers-color-scheme:dark){:root{--paper:#15171a;--surface:#1c1f23;--ink:#e9e7e3;
--muted:#9aa0a6;--rule:#2d3238;--code:#22262b;--accent:#d5a25e}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.65 "Iowan Old Style",Palatino,Georgia,serif}
.wrap{max-width:80ch;margin:0 auto;padding:48px 22px 90px}
h1,h2,h3,h4{font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.2;text-wrap:balance;margin:1.8em 0 .5em}
h1{font-size:2rem;margin-top:0;letter-spacing:-.02em}
h2{font-size:1.35rem;padding-bottom:.25em;border-bottom:1px solid var(--rule)}
h3{font-size:1.05rem}h4{font-size:.95rem;color:var(--muted)}
p{margin:0 0 1em}
ul,ol{margin:0 0 1em;padding-left:1.4em}li{margin:.25em 0}
blockquote{margin:0 0 1em;padding:.6em 1em;border-left:3px solid var(--accent);
background:var(--surface);color:var(--muted);font-style:italic}
code{font:.86em/1.5 ui-monospace,"SF Mono",Menlo,monospace;background:var(--code);
padding:.1em .35em;border-radius:3px}
pre{background:var(--code);padding:14px 16px;border-radius:5px;overflow-x:auto}
pre code{background:none;padding:0}
.tw{overflow-x:auto;margin:0 0 1.4em;border:1px solid var(--rule);border-radius:5px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font:14px/1.5 ui-sans-serif,system-ui,sans-serif}
th,td{padding:8px 11px;text-align:left;border-bottom:1px solid var(--rule);vertical-align:top}
th{background:var(--code);font-weight:600;font-size:.76rem;letter-spacing:.05em;
text-transform:uppercase;color:var(--muted);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap;
font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.9em}
hr{border:none;border-top:1px solid var(--rule);margin:2em 0}
.stamp{font:12px ui-monospace,monospace;color:var(--muted);margin-bottom:2.2em;
padding-bottom:1em;border-bottom:1px solid var(--rule)}
.gal{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin:0 0 2.4em}
figure{margin:0;background:var(--surface);border:1px solid var(--rule);border-radius:6px;overflow:hidden}
figure img{display:block;width:100%;height:auto;background:var(--code)}
figcaption{font:12.5px/1.45 ui-sans-serif,system-ui,sans-serif;color:var(--muted);
padding:9px 12px;border-top:1px solid var(--rule)}
figcaption b{color:var(--ink);font-weight:600}
"""

if __name__ == "__main__":
    src, dst, title = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    imgs = []
    a = sys.argv[4:]
    while "--img" in a:
        k = a.index("--img")
        srcpath, _, cap = a[k + 1].partition("|")
        imgs.append((srcpath, cap))
        del a[k:k + 2]
    gallery = ""
    if imgs:
        cells = "".join(
            f"<figure><img src='{html.escape(i)}' alt='{html.escape(c[:80])}' loading='lazy'>"
            f"<figcaption>{c}</figcaption></figure>" for i, c in imgs)
        gallery = f"<div class='gal'>{cells}</div>"
    md = src.read_text(encoding="utf-8", errors="replace")
    import datetime, os
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M")
    dst.write_text(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class='wrap'>"
        f"<p class='stamp'>{html.escape(src.name)} · {len(md):,} bytes · last written {ts} · "
        f"snapshot rendered {datetime.datetime.now().strftime('%H:%M')}</p>"
        f"{gallery}{render(md)}</div></body></html>", encoding="utf-8")
    print(f"{dst} ({dst.stat().st_size:,} bytes)")
