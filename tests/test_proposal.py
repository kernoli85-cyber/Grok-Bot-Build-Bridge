"""Proposal parser: untrusted model text, no source writes."""
import json

import pytest

from grok_bot_build_bridge.proposal import ProposalError, parse_replacements

SEL = ["notes/a.txt"]


def _obj(files):
    return json.dumps({"schema": "gbbb.file_replacements/v1", "files": files})


def test_rejects_malformed_schema_chatter_and_duplicate_json():
    good = [{"path": "notes/a.txt", "content": "next\n"}]
    cases = [
        ("{nope", "proposal_malformed"),
        (json.dumps({"schema": "other", "files": good}), "proposal_wrong_schema"),
        ("```json\n" + _obj(good) + "\n```", "proposal_chatter"),
        ("note\n" + _obj(good), "proposal_malformed"),
        (_obj(good) + _obj(good), "proposal_duplicate_json"),
        ("", "proposal_empty"),
        (_obj([{"path": "other.txt", "content": "x\n"}]), "proposal_wrong_path"),
        (_obj([{"path": "../a.txt", "content": "x\n"}]), "proposal_wrong_path"),
        (_obj([{"path": "notes/a.txt", "content": "x" * 9000}]), "proposal_oversize"),
        (_obj([{"path": "notes/a.txt", "content": "a\u202eb"}]), "proposal_not_utf8"),
    ]
    for text, code in cases:
        with pytest.raises(ProposalError) as exc:
            parse_replacements(text, SEL)
        assert exc.value.code == code


def test_identical_content_parses():
    found = parse_replacements(_obj([{"path": "notes/a.txt", "content": "same\n"}]), SEL)
    assert found == {"notes/a.txt": "same\n"}


def test_files_only_object_stays_wrong_schema():
    with pytest.raises(ProposalError) as exc:
        parse_replacements(json.dumps({"files": [{"path": "notes/a.txt", "content": "same\n"}]}), SEL)
    assert exc.value.code == "proposal_wrong_schema"
