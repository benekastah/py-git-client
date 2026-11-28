from conftest import utf8_encodable


def test_report_text_that_utf8_cant_encode_is_escaped_for_xdist() -> None:
    # A lone surrogate, as a path's byte that isn't UTF-8 decodes to with os.fsdecode.
    report = {"longrepr": {"lines": ["café", "caf\udce9"]}, "sections": [("out", "\udce9")]}

    assert utf8_encodable(report) == {
        "longrepr": {"lines": ["café", "caf\\udce9"]},
        "sections": [("out", "\\udce9")],
    }
    assert utf8_encodable(None) is None
    assert utf8_encodable(3) == 3
