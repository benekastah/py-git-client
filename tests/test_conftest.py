import json

import pytest
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


def test_reports_are_serialized_with_that_text_escaped(request: pytest.FixtureRequest) -> None:
    report = pytest.TestReport("t.py::t", ("t.py", 0, "t"), {}, "failed", "caf\udce9", "call")

    data = request.config.hook.pytest_report_to_serializable(config=request.config, report=report)

    assert "caf\\\\udce9" in json.dumps(data, ensure_ascii=False).encode("utf-8").decode("utf-8")
