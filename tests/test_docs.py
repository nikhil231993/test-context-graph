"""docs.py: Markdown text -> DocSections, on small made-up Markdown."""

from __future__ import annotations

from pathlib import Path

from test_context_graph.docs import parse_markdown
from test_context_graph.models import DocSection

FIXTURES = Path(__file__).parent / "fixtures" / "docs"


def parse(*lines: str) -> list[DocSection]:
    return parse_markdown("\n".join(lines) + "\n", "docs/guide.md")


def headings(sections: list[DocSection]) -> list[str]:
    return [s.heading for s in sections if s.level > 0]


def test_guide_outline():
    sections = parse_markdown((FIXTURES / "guide.md").read_text(encoding="utf-8"), "docs/guide.md")
    assert [(s.level, s.heading, s.anchor, s.start_line, s.end_line) for s in sections] == [
        (0, "", "", 1, 4),
        (1, "Bookshop service", "bookshop-service", 5, 8),
        (2, "Running locally", "running-locally", 9, 19),  # its fences hold no headings
        (3, "With Docker", "with-docker", 20, 23),
        (1, "Configuration", "configuration", 24, 28),  # setext ===
        (2, "Database", "database", 29, 35),  # setext ---; the later --- is a thematic break
        (2, "Usage", "usage", 36, 39),
        (2, "Usage", "usage-1", 40, 43),
        (2, "📦 Install the CLI (v2)!", "-install-the-cli-v2", 44, 50),
        (2, "Contributing", "contributing", 51, 52),  # ends the fence left open in a list item
    ]
    assert all(s.path == "docs/guide.md" for s in sections)


def test_a_section_runs_from_its_heading_to_the_next_heading_of_any_level():
    sections = parse("# Title", "intro", "## Sub", "sub text", "", "# Next")
    assert [(s.start_line, s.end_line, s.text) for s in sections] == [
        (1, 2, "# Title\nintro"),  # without its subsection
        (3, 5, "## Sub\nsub text\n"),
        (6, 6, "# Next"),
    ]


def test_text_before_the_first_heading_is_a_level_0_section_unless_blank():
    preamble = parse("Some intro.", "", "# Title")[0]
    assert (preamble.level, preamble.heading, preamble.anchor) == (0, "", "")
    assert (preamble.start_line, preamble.end_line, preamble.text) == (1, 2, "Some intro.\n")
    assert [s.level for s in parse("", "  ", "# Title")] == [1]
    assert [(s.level, s.end_line) for s in parse("No headings at all.", "Really.")] == [(0, 2)]


def test_breadcrumbs_are_the_parent_headings_outermost_first():
    sections = parse("# Guide", "### Deep", "## Mid", "### Leaf", "# Other", "## Child")
    assert [(s.heading, s.breadcrumbs) for s in sections] == [
        ("Guide", []),
        ("Deep", ["Guide"]),
        ("Mid", ["Guide"]),
        ("Leaf", ["Guide", "Mid"]),
        ("Other", []),
        ("Child", ["Other"]),
    ]


def test_atx_heading_rules():
    sections = parse(
        "# Title with closing hashes ##",
        "#NoSpace is a hashtag",
        "####### seven is too many",
        "    # indented code",
        "\t# tab-indented code",
        "   ###   Three spaces are fine   ",
        "#",
    )
    assert [(s.level, s.heading, s.anchor) for s in sections] == [
        (1, "Title with closing hashes", "title-with-closing-hashes"),
        (3, "Three spaces are fine", "three-spaces-are-fine"),
        (1, "", "-1"),  # an empty heading; "" stays the file's own anchor
    ]


def test_hash_lines_inside_code_fences_are_not_headings():
    sections = parse(
        "# Setup",
        "```bash",
        "# a shell comment",
        "````",  # a longer fence closes it
        "~~~~",
        "## inside tildes",
        "```",  # backticks don't close a tilde fence
        "## still inside",
        "~~~~~",
        "```inline``` code is not a fence",
        "# After",
        "```",
        "# a fence never closed runs to the end, as on GitHub",
    )
    assert headings(sections) == ["Setup", "After"]


def test_a_fence_left_open_in_a_list_item_ends_with_the_item():
    sections = parse(
        "## Build",
        "1. Package it:",
        "   ```sh",
        "   ./mvnw package",
        "   # not a heading",
        "",
        "## Deploy",
        "- Then:",
        "    ```sh",
        "  # less indented than the fence but still in the item: code",
        "    ```",
        "## Done",
    )
    assert headings(sections) == ["Build", "Deploy", "Done"]


def test_setext_headings():
    sections = parse("Title", "=====", "", "A paragraph over", "two lines", "---", "text")
    assert [(s.level, s.heading, s.start_line, s.end_line) for s in sections] == [
        (1, "Title", 1, 3),
        (2, "A paragraph over two lines", 4, 7),
    ]


def test_dashes_that_are_not_setext_underlines():
    sections = parse(
        "# Top",
        "",
        "---",  # after a blank line: a thematic break
        "- a list item",
        "---",
        "| a | b |",
        "|---|---|",
        "| 1 | 2 |",
        "---",
        "Name | Value",
        "--- | ---",
        "x | y",
        "---",
        "> a quote",
        "---",
        "    indented code",
        "---",
    )
    assert headings(sections) == ["Top"]


def test_front_matter_and_html_comments_hide_headings():
    sections = parse(
        "---",
        "title: Guide",
        "layout: page",
        "---",
        "<!--",
        "# Hidden heading",
        "-->",
        "<!-- a one-line comment -->",
        "# Shown",
    )
    assert [(s.level, s.heading, s.start_line, s.end_line) for s in sections] == [
        (0, "", 1, 8),
        (1, "Shown", 9, 9),
    ]


def test_anchors_are_github_slugs_of_the_heading_text():
    sections = parse(
        "## Running **the** service _locally_",
        "## 🚀 Quick start",
        "## [Spring Boot](https://spring.example) & `MockMvc`",
        "## C++ / Java: what's new?",
        "## snake_case_name",
        "## Ünïcödé Straße",
        '## <a name="old"></a>Anchored',
        "## Escaped \\*stars\\* &amp; entities",
    )
    assert [(s.heading, s.anchor) for s in sections] == [
        ("Running the service locally", "running-the-service-locally"),
        ("🚀 Quick start", "-quick-start"),
        ("Spring Boot & MockMvc", "spring-boot--mockmvc"),
        ("C++ / Java: what's new?", "c--java-whats-new"),
        ("snake_case_name", "snake_case_name"),
        ("Ünïcödé Straße", "ünïcödé-straße"),
        ("Anchored", "anchored"),
        ("Escaped *stars* & entities", "escaped-stars--entities"),
    ]


def test_duplicate_anchors_get_numbered_like_github():
    sections = parse("# A", "# A-1", "# A", "# A", "## B")
    assert [s.anchor for s in sections] == ["a", "a-1", "a-2", "a-3", "b"]


def test_line_endings_and_byte_order_mark():
    sections = parse_markdown("﻿Intro\r\n# A\r\ntext\r# B", "docs/x.md")
    assert [(s.heading, s.start_line, s.end_line, s.text) for s in sections] == [
        ("", 1, 1, "Intro"),
        ("A", 2, 3, "# A\ntext"),
        ("B", 4, 4, "# B"),
    ]


def test_empty_text_has_no_sections():
    assert parse_markdown("", "docs/x.md") == []
    assert parse_markdown("\n  \n", "docs/x.md") == []
