"""Markdown -> BlockNote JSON conversion.

BlockNote is the rich-text format Athena stores in node descriptions. The
conversion logic mirrors what the Athena TUI produces for typed markdown,
so a description created by this action renders identically to one written
in the TUI.

Only the markdown -> BlockNote direction is needed here (we never read
existing BlockNote). The reverse (`blocknote_to_markdown`) is intentionally
omitted; if it's ever needed, port it from the same source.
"""

from __future__ import annotations

import json
import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token


def markdown_to_blocknote(markdown: str) -> str:
    """Wrap markdown text as a BlockNote JSON string for the description field.

    The leading ``<!--blocknote-->`` prefix is how the server (and the TUI's
    ``render_blocknote``) detects BlockNote content vs plain text. Empty input
    returns an empty string (caller passes that straight through; the server
    accepts an empty description).
    """
    if not markdown:
        return ""
    blocks = _parse_markdown_to_blocks(markdown)
    return "<!--blocknote-->" + json.dumps(blocks)


def _parse_markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into BlockNote block structures using markdown-it-py."""
    md = MarkdownIt()
    tokens = md.parse(markdown)
    blocks: list[dict[str, Any]] = []
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token.type == "heading_open":
            level = int(token.tag[1])  # h1 -> 1, h2 -> 2, etc.
            level = min(level, 3)  # BlockNote supports 1-3
            inline_token = tokens[i + 1] if i + 1 < len(tokens) else None
            blocks.append(
                {
                    "type": "heading",
                    "props": {"level": level},
                    "content": _convert_inline_tokens(inline_token),
                }
            )
            i += 3
            continue

        if token.type == "paragraph_open":
            inline_token = tokens[i + 1] if i + 1 < len(tokens) else None
            if inline_token and inline_token.content:
                blocks.append(
                    {
                        "type": "paragraph",
                        "content": _convert_inline_tokens(inline_token),
                    }
                )
            i += 3
            continue

        if token.type == "bullet_list_open":
            i += 1
            while i < len(tokens) and tokens[i].type != "bullet_list_close":
                if tokens[i].type == "list_item_open":
                    item_content = _extract_list_item_content(tokens, i)
                    checkbox_match = re.match(r"^\[([ xX])\]\s*(.*)$", item_content)
                    if checkbox_match:
                        checked = checkbox_match.group(1).lower() == "x"
                        text = checkbox_match.group(2)
                        blocks.append(
                            {
                                "type": "checkListItem",
                                "props": {"checked": checked},
                                "content": _parse_inline_content(text),
                            }
                        )
                    else:
                        blocks.append(
                            {
                                "type": "bulletListItem",
                                "content": _parse_inline_content(item_content),
                            }
                        )
                i += 1
            i += 1
            continue

        if token.type == "ordered_list_open":
            i += 1
            while i < len(tokens) and tokens[i].type != "ordered_list_close":
                if tokens[i].type == "list_item_open":
                    item_content = _extract_list_item_content(tokens, i)
                    blocks.append(
                        {
                            "type": "numberedListItem",
                            "content": _parse_inline_content(item_content),
                        }
                    )
                i += 1
            i += 1
            continue

        if token.type == "blockquote_open":
            quote_content = []
            i += 1
            while i < len(tokens) and tokens[i].type != "blockquote_close":
                if tokens[i].type == "paragraph_open":
                    inline_token = tokens[i + 1] if i + 1 < len(tokens) else None
                    if inline_token and inline_token.content:
                        content_text = inline_token.content.replace("\n", " ")
                        quote_content.append(content_text)
                    i += 3
                else:
                    i += 1
            text = " ".join(quote_content)
            blocks.append(
                {
                    "type": "blockquote",
                    "content": _parse_inline_content(text),
                }
            )
            i += 1
            continue

        if token.type == "fence":
            language = token.info or ""
            code_text = token.content.rstrip("\n")
            block: dict[str, Any] = {
                "type": "codeBlock",
                "props": {},
                "content": [{"type": "text", "text": code_text, "styles": {}}],
            }
            if language:
                block["props"]["language"] = language
            blocks.append(block)
            i += 1
            continue

        if token.type == "code_block":
            code_text = token.content.rstrip("\n")
            blocks.append(
                {
                    "type": "codeBlock",
                    "props": {},
                    "content": [{"type": "text", "text": code_text, "styles": {}}],
                }
            )
            i += 1
            continue

        if token.type == "hr":
            blocks.append({"type": "divider", "props": {}})
            i += 1
            continue

        i += 1

    return blocks


def _extract_list_item_content(tokens: list[Token], start_idx: int) -> str:
    """Extract text content from a list item."""
    i = start_idx + 1
    while i < len(tokens) and tokens[i].type != "list_item_close":
        if tokens[i].type == "inline":
            return tokens[i].content
        if tokens[i].type == "paragraph_open":
            inline_token = tokens[i + 1] if i + 1 < len(tokens) else None
            if inline_token and inline_token.type == "inline":
                return inline_token.content
        i += 1
    return ""


def _convert_inline_tokens(inline_token: Token | None) -> list[dict[str, Any]]:
    """Convert markdown-it inline token to BlockNote content array."""
    if not inline_token or not inline_token.children:
        if inline_token and inline_token.content:
            return [{"type": "text", "text": inline_token.content, "styles": {}}]
        return []

    content: list[dict[str, Any]] = []
    children = inline_token.children
    i = 0
    current_styles: dict[str, bool] = {}

    while i < len(children):
        child = children[i]

        if child.type == "text":
            text = child.content
            if text:
                content.append(
                    {
                        "type": "text",
                        "text": text,
                        "styles": dict(current_styles),
                    }
                )

        elif child.type == "code_inline":
            content.append(
                {
                    "type": "text",
                    "text": child.content,
                    "styles": {"code": True},
                }
            )

        elif child.type == "strong_open":
            current_styles["bold"] = True

        elif child.type == "strong_close":
            current_styles.pop("bold", None)

        elif child.type == "em_open":
            current_styles["italic"] = True

        elif child.type == "em_close":
            current_styles.pop("italic", None)

        elif child.type == "link_open":
            href = ""
            attrs = child.attrs
            if isinstance(attrs, dict):
                href = attrs.get("href", "")
            elif attrs is not None:
                attr_list: list[tuple[str, str | int | None]] = list(attrs)  # type: ignore[arg-type]
                for attr in attr_list:
                    if attr[0] == "href":
                        href = str(attr[1]) if attr[1] else ""
                        break
            link_text_parts = []
            i += 1
            while i < len(children) and children[i].type != "link_close":
                if children[i].type == "text":
                    link_text_parts.append(children[i].content)
                i += 1
            link_text = "".join(link_text_parts)
            content.append(
                {
                    "type": "link",
                    "href": href,
                    "content": [{"type": "text", "text": link_text, "styles": {}}],
                }
            )

        elif child.type == "softbreak":
            content.append({"type": "text", "text": " ", "styles": {}})

        elif child.type == "html_inline":
            if child.content:
                content.append(
                    {
                        "type": "text",
                        "text": child.content,
                        "styles": dict(current_styles),
                    }
                )

        i += 1

    return content if content else [{"type": "text", "text": "", "styles": {}}]


def _parse_inline_content(text: str) -> list[dict[str, Any]]:
    """Parse inline markdown formatting into BlockNote content array."""
    if not text:
        return []

    md = MarkdownIt()
    tokens = md.parse(text)

    for token in tokens:
        if token.type == "inline":
            return _convert_inline_tokens(token)

    return [{"type": "text", "text": text, "styles": {}}]