"""Deterministic, offline QA archive operations (Python standard library only)."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlsplit
import uuid


SKILL_ROOT = Path(__file__).resolve().parent.parent
ENTRY_MARKER = "qa-archive:entry"
INDEX_MARKER = "qa-archive:v1"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"article", "aside", "blockquote", "br", "details", "div", "h1", "h2",
         "h3", "h4", "hr", "li", "ol", "p", "pre", "section", "summary",
         "table", "td", "th", "tr", "ul"}
ALLOWED = {
    "p", "br", "pre", "code", "ul", "ol", "li", "table", "thead", "tbody",
    "tfoot", "tr", "th", "td", "blockquote", "strong", "em", "b", "i",
    "del", "s", "a", "h3", "h4", "hr", "details", "summary",
}
LABELS = {
    "created_at": "\u65e5\u671f",
    "updated_at": "\u66f4\u65b0",
    "category": "\u5206\u7c7b",
    "keywords": "\u5173\u952e\u8bcd",
}


class ArchiveError(ValueError):
    pass


@dataclass
class Element:
    tag: str
    attrs: dict
    start: int
    inner_start: int
    inner_end: int = 0
    end: int = 0
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style", "head"}:
            return ""
        return "".join(c.text() if isinstance(c, Element) else c for c in self.children)

    def plain(self):
        if self.tag in {"script", "style", "head"}:
            return ""
        content = "".join(c.plain() if isinstance(c, Element) else c for c in self.children)
        return "\n" + content + "\n" if self.tag in BLOCK else content


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.offsets = [0]
        self.offsets.extend(m.end() for m in re.finditer("\n", source))
        self.root = Element("", {}, 0, 0)
        self.stack = [self.root]
        self.comments = []
        self.feed(source)
        self.close()
        if len(self.stack) != 1:
            raise ArchiveError("Unclosed HTML element: " + self.stack[-1].tag)

    def position(self):
        line, col = self.getpos()
        return self.offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            raise ArchiveError("Duplicate HTML attributes")
        start = self.position()
        end = start + len(self.get_starttag_text())
        node = Element(tag, dict(attrs), start, end, end, end)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            raise ArchiveError("Mismatched HTML closing tag: " + tag)
        node = self.stack.pop()
        node.inner_end = self.position()
        node.end = self.source.index(">", node.inner_end) + 1

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def handle_comment(self, data):
        self.comments.append(data.strip())


def one(nodes, description):
    if len(nodes) != 1:
        raise ArchiveError(f"Expected one {description}, found {len(nodes)}")
    return nodes[0]


def required(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArchiveError(f"{key} must be a nonempty string")
    return value


def keywords(value):
    if not isinstance(value, list) or not all(
        isinstance(k, str) and k.strip() for k in value
    ):
        raise ArchiveError("keywords must be a list of nonempty strings")
    return list(dict.fromkeys(k.strip() for k in value))


def path_parts(value, category=False):
    parts = value.replace("\\", "/").split("/")
    if not parts or (category and not 1 <= len(parts) <= 2):
        raise ArchiveError("Category must have one or two levels")
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part)):
            raise ArchiveError("Invalid Windows path component: " + repr(part))
    return parts


def contained(root, parts):
    path = root.joinpath(*parts)
    if path.resolve() != path:
        raise ArchiveError("Linked or redirected archive paths are not supported: " + str(path))
    if not path.is_relative_to(root):
        raise ArchiveError("Path escapes archive root")
    return path


def safe_fragment(source):
    doc = Document(source)

    def render(node):
        if isinstance(node, str):
            return html.escape(node, quote=False)
        if node.tag not in ALLOWED:
            raise ArchiveError("Unsupported body tag: " + node.tag)
        allowed_attrs = {"a": {"href", "title"}, "code": {"class"},
                         "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"}}
        attrs = []
        for key, value in node.attrs.items():
            if key not in allowed_attrs.get(node.tag, set()) or value is None:
                raise ArchiveError("Unsupported body attribute: " + key)
            if key == "href":
                if (re.search(r"[\x00-\x20\\]", value)
                        or value.startswith("//")
                        or urlsplit(value).scheme.lower() not in {"", "https", "http", "mailto"}):
                    raise ArchiveError("Unsupported link URL")
            elif key == "class" and not re.fullmatch(r"language-[A-Za-z0-9_+-]+", value):
                raise ArchiveError("Only language-* code classes are allowed")
            elif key in {"colspan", "rowspan"} and not re.fullmatch(r"[1-9][0-9]{0,2}", value):
                raise ArchiveError("Invalid table span")
            attrs.append(f' {key}="{html.escape(value, quote=True)}"')
        opening = "<" + node.tag + "".join(attrs) + ">"
        if node.tag in VOID:
            return opening
        return opening + "".join(render(c) for c in node.children) + f"</{node.tag}>"

    result = "".join(render(c) for c in doc.root.children)
    if not doc.root.text().strip():
        raise ArchiveError("Body fragment must contain text")
    return result


def read_entry(path, root):
    raw = path.read_bytes()
    source = raw.decode("utf-8-sig")
    try:
        doc = Document(source)
        if ENTRY_MARKER not in doc.comments:
            raise ArchiveError("Missing entry marker")
        all_nodes = list(doc.root.walk())
        article = one([n for n in all_nodes if n.tag == "article"], "article")
        titles = [n for n in article.walk() if n.attrs.get("data-qa") == "title"]
        title = one(titles or [n for n in article.walk() if n.tag == "h1"], "title")
        meta = one([n for n in article.walk()
                    if n.tag == "dl" and "meta" in (n.attrs.get("class") or "").split()], "metadata")
        fields = {}
        for key, label in LABELS.items():
            matches = [n for n in meta.walk() if n.attrs.get("data-qa") == key]
            if not matches:
                for div in meta.children:
                    if not isinstance(div, Element):
                        continue
                    labels = [n for n in div.children if isinstance(n, Element) and n.tag == "dt"]
                    if len(labels) == 1 and labels[0].text().strip() == label:
                        matches.extend(n for n in div.children
                                       if isinstance(n, Element) and n.tag == "dd")
            if matches or key != "updated_at":
                fields[key] = one(matches, key)
        created = fields["created_at"].text().strip()
        updated = fields.get("updated_at", fields["created_at"]).text().strip()
        for value in (created, updated):
            if date.fromisoformat(value).isoformat() != value:
                raise ArchiveError("Dates must use YYYY-MM-DD")
        if updated < created:
            raise ArchiveError("Update date precedes creation date")
        category = "/".join(path_parts(fields["category"].text().strip(), category=True))
        kw_node = fields["keywords"]
        kw_spans = [n for n in kw_node.walk() if "kw" in (n.attrs.get("class") or "").split()]
        kws = [n.text().strip() for n in kw_spans] if kw_spans else [
            k.strip() for k in re.split("[,\uff0c]", kw_node.text()) if k.strip()
        ]
        ids = [n for n in all_nodes if n.tag == "meta" and n.attrs.get("name") == "qa-id"]
        relative = path.relative_to(root).as_posix()
        entry_id = (one(ids, "qa-id").attrs.get("content") if ids
                    else str(uuid.uuid5(uuid.NAMESPACE_URL, relative)))
        if not entry_id or not title.text().strip():
            raise ArchiveError("Empty ID or title")
        body = "".join(
            c.plain() if isinstance(c, Element) else c for c in article.children
            if not isinstance(c, Element) or (
                c is not title and c is not meta
                and "back" not in (c.attrs.get("class") or "").split())
        )
        entry = {
            "id": entry_id, "title": title.text().strip(), "category": category,
            "date": created, "created_at": created, "updated_at": updated,
            "keywords": keywords(kws), "file": relative,
            "path": quote(relative, safe="/"),
            "text": " ".join(body.split()),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        return entry, doc, article, meta, fields
    except (ValueError, KeyError) as exc:
        raise ArchiveError(f"{path}: {exc}") from exc


def entries(root):
    result = []

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in dirs:
            contained(root, list((Path(directory) / name).relative_to(root).parts))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() != ".html" or path == root / "index.html":
                continue
            contained(root, list(path.relative_to(root).parts))
            raw = path.read_bytes()
            if b"<!-- qa-archive:entry -->" in raw:
                result.append(read_entry(path, root)[0])
    ids = [e["id"] for e in result]
    if len(set(ids)) != len(ids):
        raise ArchiveError("Duplicate entry IDs; resolve copied entries before rebuilding")
    return result


def check_index(root):
    path = contained(root, ["index.html"])
    if path.exists():
        doc = Document(path.read_text(encoding="utf-8-sig"))
        if INDEX_MARKER not in doc.comments:
            raise ArchiveError("Refusing to overwrite unmarked index: " + str(path))


@contextmanager
def archive_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".qa-archive.lock"
    with lock.open("x", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink()


def publish(path, source, replace=True, expected=None):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=".qa-", suffix=".tmp", delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ArchiveError("Entry changed during update; scan and review again: " + str(path))
        if replace:
            os.replace(temp, path)
        elif os.name == "nt":
            os.rename(temp, path)
        else:
            os.link(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def template(name, values):
    source = (SKILL_ROOT / name).read_text(encoding="utf-8")

    def substitute(match):
        key = match[1]
        if key not in values:
            raise ArchiveError("Unknown template placeholder: " + key)
        return values[key]

    return re.sub(r"\{\{([A-Z_]+)\}\}", substitute, source)


def keyword_html(kws):
    return " ".join('<span class="kw">' + html.escape(k) + "</span>" for k in kws)


def rebuild(root):
    check_index(root)
    data = entries(root)
    serialized = json.dumps(data, ensure_ascii=True)
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    source = template("index-template.html", {"ENTRIES_JSON": serialized})
    Document(source)
    publish(root / "index.html", source)
    return {"status": "rebuilt", "count": len(data), "index": str(root / "index.html")}


def create(root, data):
    title = required(data, "title")
    category = "/".join(path_parts(required(data, "category"), category=True))
    kws = keywords(data.get("keywords"))
    lang = data.get("lang", "zh")
    if not isinstance(lang, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", lang):
        raise ArchiveError("Invalid lang tag")
    question = safe_fragment(required(data, "question_html"))
    answer = safe_fragment(required(data, "answer_html"))
    today = date.today().isoformat()
    slug = re.sub(r'[<>:"/\\|?*\s\x00-\x1f]+', "-", title).strip(" .-")[:70].rstrip(" .-") or "qa"
    folder = contained(root, category.split("/"))
    folder.mkdir(parents=True, exist_ok=True)
    stem = today + "-" + slug
    path = folder / (stem + ".html")
    number = 2
    while path.exists():
        path = folder / f"{stem}-{number}.html"
        number += 1
    contained(root, list(path.relative_to(root).parts))
    source = template("entry-template.html", {
        "TITLE": html.escape(title), "CATEGORY": html.escape(category),
        "DATE": today, "ID": str(uuid.uuid4()), "LANG": lang,
        "KEYWORDS_HTML": keyword_html(kws), "QUESTION": question, "ANSWER": answer,
        "SUPPLEMENTS": "", "INDEX_HREF": "../" * len(category.split("/")) + "index.html",
    })
    Document(source)
    publish(path, source, replace=False)
    return {"status": "created", "file": str(path)}


def update(root, data):
    path = contained(root, path_parts(required(data, "target")))
    if path.suffix.lower() != ".html" or path == root / "index.html":
        raise ArchiveError("Update target must be an entry HTML file")
    expected = required(data, "expected_sha256")
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ArchiveError("expected_sha256 must come from scan")
    entry, doc, article, meta, fields = read_entry(path, root)
    content = safe_fragment(required(data, "content_html"))
    kws = keywords(data.get("keywords"))
    action = data["action"]
    note = required(data, "correction_note") if action == "correction" else ""
    fingerprint = hashlib.sha256(json.dumps(
        [action, content, note, sorted(kws)], ensure_ascii=True
    ).encode("utf-8")).hexdigest()
    if any(n.attrs.get("data-qa-operation") == fingerprint for n in article.walk()):
        return {"status": "skipped", "file": str(path)}
    if entry["sha256"] != expected:
        raise ArchiveError("Entry changed; scan and review again: " + str(path))
    today = date.today().isoformat()
    if today < entry["updated_at"]:
        raise ArchiveError("Local date precedes the entry update date")
    label = "\u66f4\u6b63" if action == "correction" else "\u8865\u5145"
    anchor = "qa-" + fingerprint
    section = (f'\n<section class="supplement" id="{anchor}" data-qa-operation="{fingerprint}">'
               f"<h2>{label} ({today})</h2>")
    if note:
        section += "<p><strong>" + html.escape(note) + "</strong></p>"
    section += "<div>" + content + "</div></section>\n"
    edits = [(article.inner_end, article.inner_end, section)]
    kw_node = fields["keywords"]
    edits.append((kw_node.inner_start, kw_node.inner_end, keyword_html(
        keywords(entry["keywords"] + kws))))
    if "updated_at" in fields:
        node = fields["updated_at"]
        edits.append((node.inner_start, node.inner_end, today))
    else:
        edits.append((meta.inner_end, meta.inner_end,
                      f'<div><dt>{LABELS["updated_at"]}</dt>'
                      f'<dd data-qa="updated_at">{today}</dd></div>\n'))
    if not any(n.tag == "meta" and n.attrs.get("name") == "qa-id" for n in doc.root.walk()):
        head = one([n for n in doc.root.walk() if n.tag == "head"], "head")
        edits.append((head.inner_end, head.inner_end,
                      f'<meta name="qa-id" content="{html.escape(entry["id"])}">\n'))
    if note:
        warning = "\u539f\u7b54\u6848\u5b58\u5728\u5df2\u5931\u6548\u7ed3\u8bba\uff0c\u8bf7\u53c2\u9605\u66f4\u6b63"
        edits.append((article.inner_start, article.inner_start,
                      f'\n<aside class="supplement"><a href="#{anchor}">{warning}</a>: '
                      + html.escape(note) + "</aside>\n"))
    source = doc.source
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    Document(source)
    publish(path, source, expected=expected)
    return {"status": action, "file": str(path)}


def write(root, data):
    if not isinstance(data, dict):
        raise ArchiveError("Input must be a JSON object")
    action = data.get("action")
    if not isinstance(action, str) or action not in {"create", "supplement", "correction"}:
        raise ArchiveError("action must be create, supplement or correction")
    allowed = ({"action", "title", "category", "keywords", "lang", "question_html", "answer_html"}
               if action == "create" else
               {"action", "target", "expected_sha256", "keywords", "content_html"})
    if action == "correction":
        allowed.add("correction_note")
    if data.keys() - allowed:
        raise ArchiveError("Unknown input fields: " + ", ".join(sorted(data.keys() - allowed)))
    check_index(root)
    entries(root)
    result = create(root, data) if action == "create" else update(root, data)
    try:
        result["index"] = rebuild(root)["index"]
    except (OSError, ValueError) as exc:
        raise ArchiveError(
            f"Entry is saved at {result['file']}; index rebuild failed: {exc}. "
            "Fix the cause and run rebuild; do not create this entry again."
        ) from exc
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "write", "rebuild"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "scan":
            if not root.is_dir():
                raise ArchiveError("Archive root does not exist: " + str(root))
            result = entries(root)
        else:
            if args.command == "write" and args.input is None:
                raise ArchiveError("write requires --input")
            data = json.loads(args.input.read_text(encoding="utf-8-sig")) if args.command == "write" else None
            with archive_lock(root):
                result = write(root, data) if args.command == "write" else rebuild(root)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
