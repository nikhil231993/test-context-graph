"""Markdown text -> DocSections, one per heading, so an agent can be shown just the part of a
README or design doc it needs. Pure: text in, sections out.

Headings are found the way GitHub renders them, as far as READMEs need: ATX (#) and setext
(=== / ---) headings, but no lines inside fenced code blocks, HTML comments or front matter.
Anchors are GitHub's, so they match the links people write.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

from .models import DocSection

_LINE_BREAK = re.compile(r"\r\n?|\n")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_ATX_CLOSING = re.compile(r"(?:^|[ \t]+)#+$")  # "## Title ##"
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_THEMATIC_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
# HTML whose content is not Markdown until its end marker: a comment, or <pre> and the like
_RAW_HTML = re.compile(r"^ {0,3}(?:<!--|<(pre|script|style|textarea)(?=[\s>]|$))", re.IGNORECASE)
_YAML_KEY = re.compile(r"^[\w-]+[ \t]*:(?:[ \t]|$)")

# Inline Markdown in a heading, reduced to the text GitHub shows (and makes the anchor from)
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]*:[^<>\s]*)>")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
# * and ~~ emphasis may sit inside a word; _ emphasis may not, so snake_case_names stay whole
_EMPHASIS = re.compile(
    r"(\*\*|\*|~~)(?=\S)(.+?)(?<=\S)\1|(?<![^\W_])(__|_)(?=\S)(.+?)(?<=\S)\3(?![^\W_])"
)
_SHIELDED = re.compile("\x00(\\d+)\x00")


def parse_markdown(text: str, path: str) -> list[DocSection]:
    """One section per heading, from its line to the line before the next heading of any level
    (so subsections are sections of their own), plus a level 0 section for any text before the
    first heading."""
    lines = _LINE_BREAK.split(text.removeprefix("\ufeff"))
    if lines[-1] == "":
        lines.pop()  # the line break that ends the last line
    headings = _find_headings(lines)
    sections = []
    first = headings[0].index if headings else len(lines)
    if any(line.strip() for line in lines[:first]):
        sections.append(
            DocSection(path=path, heading="", anchor="", level=0, start_line=1, end_line=first,
                       text="\n".join(lines[:first]))
        )
    slugger = _Slugger()
    parents: list[tuple[int, str]] = []  # (level, heading) of the headings above this one
    for n, heading in enumerate(headings):
        end = headings[n + 1].index if n + 1 < len(headings) else len(lines)
        plain = _plain(heading.raw)
        title = " ".join(plain.split())
        while parents and parents[-1][0] >= heading.level:
            parents.pop()
        sections.append(
            DocSection(
                path=path,
                heading=title,
                anchor=slugger.slug(plain),
                level=heading.level,
                start_line=heading.index + 1,
                end_line=end,
                text="\n".join(lines[heading.index:end]),
                breadcrumbs=[parent for _, parent in parents],
            )
        )
        parents.append((heading.level, title))
    return sections


@dataclass
class _Heading:
    index: int  # 0-based index of the heading's first line
    level: int
    raw: str  # its inline Markdown: "Using **H2** [console](h2.md)"


def _find_headings(lines: list[str]) -> list[_Heading]:
    """The headings, skipping lines GitHub doesn't render as headings. A small line scanner, not
    a CommonMark parser: it tracks just enough (code fences, list items, paragraphs) for that."""
    headings: list[_Heading] = []
    fence = ""  # the open code fence's marker, "```" or "~~~~"; "" outside one
    fence_floor = 0  # a non-blank line indented less ends the list item holding the fence, and it
    raw_html_end = ""  # inside raw HTML (a comment, <pre>...): the marker that ends it, "-->"
    items: list[int] = []  # content indents of the open list items, innermost last
    para: int | None = None  # first line of the paragraph above, if a setext underline can follow
    in_para = False  # a paragraph is open, so the next line may be its lazy continuation

    for i in range(_front_matter_end(lines), len(lines)):
        line = lines[i]
        stripped = line.strip()
        indent = _indent(line)
        if fence:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
                continue
            if not stripped or indent >= fence_floor:
                continue
            fence = ""  # dedented out of its list item: GitHub ends the code block with the item
        if raw_html_end:
            if raw_html_end in line.lower():
                raw_html_end = ""
            continue
        if not stripped:
            para, in_para = None, False
            continue
        if para is not None and _SETEXT.match(line):
            text = " ".join(part.strip() for part in lines[para:i])
            headings.append(_Heading(para, 1 if stripped[0] == "=" else 2, text))
            para, in_para = None, False
            continue

        opener = _FENCE.match(line)
        if opener and opener.group(1)[0] == "`" and "`" in opener.group(2):
            opener = None  # ```inline code``` on a line of its own, not a fence
        raw_html = _RAW_HTML.match(line)
        atx = _ATX.match(line)
        rule = _THEMATIC_BREAK.match(line)
        item = None if rule else _LIST_ITEM.match(line)
        if not in_para or opener or atx or rule or item or stripped[0] == ">":
            # not a lazy continuation of a paragraph: close the list items it isn't indented into
            while items and indent < items[-1]:
                items.pop()

        if opener:
            fence, fence_floor = opener.group(1), (items[-1] if items else 0)
            para, in_para = None, False
        elif raw_html:
            end = f"</{raw_html.group(1).lower()}>" if raw_html.group(1) else "-->"
            raw_html_end = "" if end in line[raw_html.end():].lower() else end
            para, in_para = None, False
        elif atx:
            headings.append(_Heading(i, len(atx.group(1)), _ATX_CLOSING.sub("", atx.group(2) or "")))
            para, in_para = None, False
        elif rule:
            para, in_para = None, False
        elif item:
            items.append(len(item.group(0).expandtabs(4)))
            para, in_para = None, True  # a setext underline can't turn a list item into a heading
        elif not in_para:
            # A new paragraph. It can become a setext heading unless it is indented code, a
            # table, a quote, HTML, or inside a list item.
            in_para = True
            para = i if not items and indent <= 3 and stripped[0] not in "|><" else None
        elif para is not None and "|" in stripped and set(stripped) <= set("|:- \t"):
            para = None  # a table's delimiter row: the paragraph above is the table's header
    return headings


def _front_matter_end(lines: list[str]) -> int:
    """The index after a leading YAML front matter block (Jekyll, Hugo), whose closing --- is not
    a setext underline; 0 when there is none."""
    if len(lines) > 1 and lines[0].rstrip() == "---" and _YAML_KEY.match(lines[1]):
        for i in range(1, len(lines)):
            if lines[i].rstrip() in ("---", "..."):
                return i + 1
    return 0


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _plain(raw: str) -> str:
    """A heading's text as GitHub shows it: links, images, code marks, emphasis and HTML tags
    reduced to their text."""
    shielded: list[str] = []

    def shield(text: str) -> str:  # code spans and escaped characters are literal text
        shielded.append(text)
        return f"\x00{len(shielded) - 1}\x00"

    text = _CODE_SPAN.sub(lambda m: shield(m.group(2).strip()), raw)
    text = _ESCAPED.sub(lambda m: shield(m.group(1)), text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    while True:  # again, for nested emphasis: ***both***, **_mixed_**
        unwrapped = _EMPHASIS.sub(lambda m: m.group(2) if m.group(1) else m.group(4), text)
        if unwrapped == text:
            break
        text = unwrapped
    text = html.unescape(text)
    return _SHIELDED.sub(lambda m: shielded[int(m.group(1))], text).strip()


class _Slugger:
    """GitHub's heading anchors (github-slugger): a repeated slug gets -1, -2..."""

    def __init__(self) -> None:
        # "" is taken: an empty anchor means the file itself (doc_id without an anchor)
        self._seen: dict[str, int] = {"": 0}

    def slug(self, heading: str) -> str:
        base = slug = _slug(heading)
        while slug in self._seen:
            self._seen[base] += 1
            slug = f"{base}-{self._seen[base]}"
        self._seen[slug] = 0
        return slug


def _slug(text: str) -> str:
    """Lower case, punctuation and symbols (emoji too) dropped, each space a '-':
    "📖 OpenAPI (v3)" -> "-openapi-v3"."""
    return "".join(ch for ch in text.lower() if _in_slug(ch)).replace(" ", "-")


def _in_slug(ch: str) -> bool:
    category = unicodedata.category(ch)
    return ch in " -_" or category[0] in "LM" or category == "Nd"
