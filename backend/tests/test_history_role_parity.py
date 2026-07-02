"""Conversation history fed to the parser must use real roles, not position.

The parser derived roles by array parity (i % 2). A turn that stores a user
message with no matching assistant reply (clarification / no-data / invalid)
leaves two consecutive user entries, after which parity flips every later role
and degrades follow-up detection. The parser now uses the stored role when
present, falling back to parity only for legacy bare-string entries.
"""

from backend.services.openrouter import _history_role_and_content as rc


def test_real_roles_preserved_through_desync():
    hist = [
        {"role": "user", "content": "US GDP"},
        {"role": "user", "content": "not what I meant"},   # user-only clarification turn
        {"role": "assistant", "content": "Here is US GDP"},
        {"role": "user", "content": "now for Canada"},
    ]
    roles = [rc(e, i)[0] for i, e in enumerate(hist)]
    # Parity would give user/assistant/user/assistant — wrong after the desync.
    assert roles == ["user", "user", "assistant", "user"]


def test_content_extracted_from_dict():
    assert rc({"role": "assistant", "content": "hi"}, 3) == ("assistant", "hi")


def test_legacy_string_falls_back_to_parity():
    assert rc("hello", 0) == ("user", "hello")
    assert rc("world", 1) == ("assistant", "world")


def test_dict_without_role_falls_back_to_parity():
    assert rc({"content": "x"}, 0) == ("user", "x")
    assert rc({"content": "y", "role": "weird"}, 1) == ("assistant", "y")
