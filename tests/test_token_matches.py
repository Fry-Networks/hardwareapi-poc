"""Regression test for constant-time bearer-token comparison.

Every bearer-token check in app.py used to compare the caller-supplied token
against the server's expected token with plain ==/!=, which short-circuits at
the first differing byte and leaks timing information an attacker can use to
recover a valid token character-by-character. `_token_matches` wraps
secrets.compare_digest so every call site gets a constant-time comparison.

Requires the app's dependencies (fastapi, slowapi, ...) to import app.py.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import _token_matches
except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"app.py dependencies not installed: {exc}", allow_module_level=True)


def test_matching_tokens_are_accepted():
    assert _token_matches("a-real-token-value", "a-real-token-value") is True


def test_mismatched_tokens_are_rejected():
    assert _token_matches("wrong-token", "a-real-token-value") is False


def test_prefix_matching_tokens_are_rejected():
    # A naive == would already reject this too, but this is exactly the case
    # a timing attack tries to exploit byte-by-byte - make sure it's still
    # a clean reject through the constant-time path.
    assert _token_matches("a-real-token-valuX", "a-real-token-value") is False


def test_empty_or_missing_tokens_never_match():
    assert _token_matches("", "a-real-token-value") is False
    assert _token_matches("a-real-token-value", "") is False
    assert _token_matches(None, "a-real-token-value") is False
    assert _token_matches("a-real-token-value", None) is False
    assert _token_matches(None, None) is False
