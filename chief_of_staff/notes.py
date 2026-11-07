"""PDF-only research notes.

`render_pdf` turns a markdown draft into a PDF and refuses any `.md` output. The draft
lives outside the destination folder, so no markdown stands beside the PDF.
Pandoc builds one styled HTML page and headless Chrome prints it. If Chrome is missing
or fails, pandoc with a LaTeX engine renders the same draft. If every engine fails, the
call raises and writes nothing.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_S = 120

CSS = """
@page { size: A4; margin: 20mm 18mm; }
html { font-size: 10.5pt; }
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.45; color: #1a1a1a; margin: 0; }
h1 { font-family: Helvetica, Arial, sans-serif; font-size: 20pt; margin: 0 0 6pt; }
h2 { font-family: Helvetica, Arial, sans-serif; font-size: 13.5pt; margin: 16pt 0 4pt;
     border-bottom: 0.6pt solid #bbb; padding-bottom: 2pt; page-break-after: avoid; }
h3 { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; margin: 10pt 0 2pt; }
p { margin: 0 0 6pt; orphans: 3; widows: 3; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9.5pt; }
th, td { border: 0.5pt solid #bbb; padding: 3pt 5pt; text-align: left; vertical-align: top; }
code, pre { background: #f4f4f4; font-size: 9pt; }
pre { padding: 6pt; white-space: pre-wrap; }
blockquote { margin: 6pt 0; padding-left: 10pt; border-left: 2pt solid #bbb; color: #444; }
"""


class RenderError(RuntimeError):
    pass


def chrome_path():
    env = os.environ.get("CHIEF_CHROME")
    if env and Path(env).is_file():
        return env
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).is_file():
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def pandoc_path():
    found = shutil.which("pandoc")
    if not found:
        raise RenderError("pandoc is not installed")
    return found


def split_title(text):
    """The frontmatter title or the first heading, and the body."""
    title = ""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if m:
        t = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", m.group(1), re.M)
        title = t.group(1) if t else ""
        text = text[m.end():]
    if not title:
        h = re.search(r"^#\s+(.+)$", text, re.M)
        title = h.group(1).strip() if h else ""
    return title, text.lstrip("\n")


def check_destination(out, src=None):
    out = Path(out)
    if out.suffix.lower() != ".pdf":
        raise RenderError(
            "%s: a note is delivered as a .pdf, never as %s" % (out.name, out.suffix or "a bare file")
        )
    folder = out.parent.resolve()
    if src is not None and Path(src).resolve().parent == folder:
        raise RenderError(
            "%s: the draft sits in the destination folder; keep drafts in a temporary folder" % src
        )
    if (folder / (out.stem + ".md")).exists():
        raise RenderError("a markdown file with the same name stands beside the PDF")


def _html_page(body, title, work):
    (work / "note.md").write_text(body, encoding="utf-8")
    cmd = [pandoc_path(), "-f", "markdown+pipe_tables-implicit_figures", "-t", "html5", "-s",
           "--metadata", "pagetitle=" + (title or "Note"), "-o", str(work / "note.html"),
           str(work / "note.md")]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=work)
    if run.returncode != 0:
        raise RenderError("pandoc failed: " + (run.stderr or run.stdout).strip()[:200])
    page = (work / "note.html").read_text(encoding="utf-8")
    page = page.replace("</head>", "<style>" + CSS + "</style></head>", 1)
    (work / "note.html").write_text(page, encoding="utf-8")
    return work / "note.html"


def chrome_engine(body, title, pdf, work):
    chrome = chrome_path()
    if not chrome:
        raise RenderError("Chrome is not installed")
    page = _html_page(body, title, work)
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
           "--user-data-dir=" + str(work / "profile"), "--print-to-pdf=" + str(pdf), page.as_uri()]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    if not pdf.is_file() or pdf.stat().st_size < 500:
        raise RenderError("Chrome wrote no PDF: " + (run.stderr or "")[-200:].strip())


def latex_engine(body, title, pdf, work):
    (work / "latex.md").write_text(body, encoding="utf-8")
    cmd = [pandoc_path(), "-f", "markdown+pipe_tables", "--pdf-engine=xelatex", "-V",
           "geometry:margin=20mm", "--metadata", "title=" + (title or "Note"), "-o", str(pdf),
           str(work / "latex.md")]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=work)
    if run.returncode != 0 or not pdf.is_file():
        raise RenderError("LaTeX fallback failed: " + (run.stderr or run.stdout).strip()[:200])


ENGINES = (("chrome", chrome_engine), ("latex", latex_engine))


def is_pdf(path):
    p = Path(path)
    try:
        with p.open("rb") as handle:
            return handle.read(5) == b"%PDF-" and p.stat().st_size > 500
    except OSError:
        return False


def render_pdf(markdown, out, *, title="", src=None, engines=None):
    """Write `out` as a PDF made from `markdown`. Returns path, engine and size."""
    out = Path(out).expanduser()
    check_destination(out, src)
    if not markdown.strip():
        raise RenderError("the note is empty")
    found, body = split_title(markdown)
    title = title or found
    out.parent.mkdir(parents=True, exist_ok=True)
    errors, engine_name = [], ""
    with tempfile.TemporaryDirectory(prefix="chief-note-") as tmp:
        work = Path(tmp)
        staged = work / "out.pdf"
        for name, engine in engines or ENGINES:
            try:
                engine(body, title, staged, work)
                engine_name = name
                break
            except (RenderError, subprocess.SubprocessError, OSError) as exc:
                errors.append("%s: %s" % (name, exc))
                staged.unlink(missing_ok=True)
        if not engine_name:
            raise RenderError("no renderer worked: " + " | ".join(errors))
        if staged.read_bytes()[:5] != b"%PDF-":
            raise RenderError("the renderer wrote a file that is not a PDF")
        shutil.copyfile(staged, out)
    return dict(path=str(out), engine=engine_name, bytes=out.stat().st_size, title=title)
