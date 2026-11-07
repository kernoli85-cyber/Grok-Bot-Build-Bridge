"""Proposal parser: untrusted model text, no source writes."""
import json

import pytest

from grok_bot_build_bridge.proposal import (
    ProposalError, model_text_diagnostic, parse_literal, parse_replacements,
)

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


def test_diagnostic_categories_do_not_include_text():
    prose = "def add(a, b):\n    return a + b\n"
    diag = model_text_diagnostic(prose, extraction_key="text", stdout_truncated=False)
    blob = json.dumps(diag)
    assert diag["leading"] == "letter" and diag["json"] == "expecting_value" and diag["json_offset"] == 0
    assert diag["fence"] == "none" and diag["fenced_json"] is False
    assert diag["stdout_capture_truncated"] is False
    assert "def add" not in blob and "return" not in blob
    fenced = "```json\n" + _obj([{"path": "notes/a.txt", "content": "beta\n"}]) + "\n```"
    wrapped = model_text_diagnostic(fenced)
    assert wrapped["fence"] == "backtick" and wrapped["fenced_json"] is True
    with pytest.raises(ProposalError) as exc:
        parse_replacements(fenced, SEL)
    assert exc.value.code == "proposal_chatter"
    assert "notes/a.txt" not in json.dumps(wrapped) and "beta" not in json.dumps(wrapped)
    partial = '{"schema":"gbbb.file_replacements/v1","files":['
    cut = model_text_diagnostic(partial, extraction_key="result")
    assert cut["leading"] == "object" and cut["extraction_key"] == "result"
    assert cut["json"] in {"unterminated_string", "expecting_delimiter", "expecting_value", "expecting_property", "json_decode"}
    assert isinstance(cut["json_offset"], int)
    assert "gbbb.file_replacements" not in json.dumps(cut)


def test_quote_escaping_json_is_malformed_and_literal_preserves_bytes():
    bad = (
        '{"schema":"gbbb.file_replacements/v1","files":[{"path":"calc.py","content":'
        + '"' * 4
        + "Synthetic calculator fixture."
        + '\\"\\"\\"'
        + "\\n\\n\\ndef add(a, b):\\n    return a + b\\n\\n\\ndef mul(a, b):\\n    return a * b\\n\"}]}"
    )
    assert len(bad) == 196
    with pytest.raises(json.JSONDecodeError) as decoded:
        json.loads(bad)
    assert decoded.value.pos == 77
    with pytest.raises(ProposalError) as exc:
        parse_replacements(bad, ["calc.py"])
    assert exc.value.code == "proposal_malformed"
    rich = "\n".join([
        '"""Synthetic calculator fixture."""',
        "",
        "def add(a, b):",
        "    return a + b",
        "",
        "def mul(a, b):",
        "    return a * b",
        "",
        "def show(s):",
        '    return "say \\"hi\\"\\n" + s',
        "",
        'NOTE = r"line\\nbreak"',
        'MARK = "```not a fence```"',
        'WORD = "café ✓"',
        "QUOTES = " + chr(39) + '"' * 4 + chr(39),
        "",
    ])
    assert '""""' in rich and "```" in rich and "\\n" in rich and "café" in rich
    nonce = "0123456789abcdef"
    framed = _frame(nonce, [("calc.py", rich)])
    assert parse_literal(framed, ["calc.py"], nonce) == {"calc.py": rich}
    no_nl = rich.rstrip("\n")
    assert parse_literal(_frame(nonce, [("calc.py", no_nl)]), ["calc.py"], nonce)["calc.py"] == no_nl
    collided = rich + f"\nFILE {nonce} path=other.py\n"
    with pytest.raises(ProposalError) as exc:
        parse_literal(_frame(nonce, [("calc.py", collided)]), ["calc.py"], nonce)
    assert exc.value.code == "proposal_delimiter_collision"
    with pytest.raises(ProposalError) as exc:
        parse_literal(_frame(nonce, [("calc.py", rich), ("extra.py", "x\n")]), ["calc.py"], nonce)
    assert exc.value.code == "proposal_wrong_path"
    with pytest.raises(ProposalError) as exc:
        parse_literal(_frame(nonce, [("calc.py", rich)]), ["calc.py", "other.py"], nonce)
    assert exc.value.code == "proposal_partial"
    with pytest.raises(ProposalError) as exc:
        parse_literal("note\n" + framed, ["calc.py"], nonce)
    assert exc.value.code == "proposal_chatter"


def _frame(nonce, items):
    chunks = [f"GBBB-REPLACEMENTS 1 {nonce}"]
    for path, content in items:
        chunks.append(f"FILE {nonce} path={path}\n{content}")
        chunks.append(f"END {nonce} path={path}")
    chunks.append(f"END-REPLACEMENTS {nonce}")
    return "\n".join(chunks) + "\n"


def test_files_only_object_stays_wrong_schema():
    with pytest.raises(ProposalError) as exc:
        parse_replacements(json.dumps({"files": [{"path": "notes/a.txt", "content": "same\n"}]}), SEL)
    assert exc.value.code == "proposal_wrong_schema"
