"""Bounded, explicitly inexact reservation estimates; never billed as actual usage.

No provider tokenizer is shipped with Admin. Ordinary Latin text is estimated in
three-character pieces, digit runs in two-character pieces, common CJK characters
and punctuation individually. Long unbroken identifiers and uncommon Unicode fall
back to their UTF-8 byte length. Include input-bearing JSON structure, message
framing, a 30% margin, and 1,024 additional protocol tokens. The caller reserves
the entire configured output limit and reconciles against provider usage.
"""

import json
import math
import re
from dataclasses import dataclass

INPUT_FIELDS = ("messages", "tools", "tool_choice", "response_format")
PIECES = re.compile(r"[A-Za-z]+|[0-9]+|\s+|.", re.DOTALL)
OPAQUE_RUNS = re.compile(r"[A-Za-z0-9_+/=\-]{40,}")
ESTIMATOR = "unicode_heuristic_v1_margin30_buffer1024"
COMMON_CJK_PUNCTUATION = frozenset("，。！？；：（）【】《》“”‘’、")


@dataclass(frozen=True)
class ReservationEstimate:
    estimated_input_tokens: int
    max_output_tokens: int
    estimator: str = ESTIMATOR
    exact: bool = False

    @property
    def required_tokens(self):
        return self.estimated_input_tokens + self.max_output_tokens

    def details(self):
        return {
            "required_tokens": self.required_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "estimator": self.estimator,
            "exact": self.exact,
            "retryable": False,
        }


def estimate_text_tokens(text):
    total = 0
    start = 0
    for match in OPAQUE_RUNS.finditer(text):
        total += estimate_ordinary_text(text[start:match.start()]) + len(match[0])
        start = match.end()
    return total + estimate_ordinary_text(text[start:])


def estimate_ordinary_text(text):
    total = 0
    for piece in PIECES.findall(text):
        if piece.isascii() and piece.isalpha():
            # Long opaque identifiers are not assumed to tokenize as prose.
            total += len(piece) if len(piece) > 24 else math.ceil(len(piece) / 3)
        elif piece.isascii() and piece.isdigit():
            total += math.ceil(len(piece) / 2)
        elif piece.isascii() and piece.isspace():
            total += sum(character != " " for character in piece)
            total += math.ceil(piece.count(" ") / 4)
        elif len(piece) == 1 and ("\u4e00" <= piece <= "\u9fff" or piece in COMMON_CJK_PUNCTUATION):
            total += 1
        else:
            # Includes syntax punctuation, emoji and rare scripts.
            total += len(piece.encode("utf-8"))
    return total


def estimate_input_structure(value):
    """Count content once, not HTTP JSON's additional escaping of that content."""
    if isinstance(value, str):
        return estimate_text_tokens(value) + 2
    if isinstance(value, dict):
        return 2 + sum(estimate_text_tokens(str(key)) + 3 + estimate_input_structure(item)
                       for key, item in value.items())
    if isinstance(value, list):
        return 2 + sum(estimate_input_structure(item) + 1 for item in value)
    return estimate_text_tokens(json.dumps(value))


def estimate_reservation(body, max_output_tokens):
    inputs = {field: body[field] for field in INPUT_FIELDS if field in body}
    base = estimate_input_structure(inputs) + len(body.get("messages", [])) * 16 + 8
    return ReservationEstimate(math.ceil(base * 1.30) + 1024, max_output_tokens)
