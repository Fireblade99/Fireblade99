# ruff: noqa: E501
from qlik_gateway.services.scriptlog import extract_script_error

RU = """20260929T140500.100+0500 Execution started.
20260929T140501.200+0500 LIB CONNECT TO 'mysql_dwh'
20260929T140502.300+0500 Произошла следующая ошибка:
20260929T140502.300+0500 Connector connect error: SQL##f - SqlState: S1000, ErrorCode: 1045, ErrorMsg: [MySQL][ODBC 9.2(w) Driver]Access denied for user
20260929T140502.400+0500 Execution Failed
20260929T140502.500+0500 Execution finished.
"""

EN = """20260929T140500 Execution started.
20260929T140502 The following error occurred:
20260929T140502 Field 'X' not found
20260929T140502 ---
20260929T140502 Execution Failed
"""


def test_russian_marker():
    err = extract_script_error(RU)
    assert err.startswith("Connector connect error") and "Access denied for user" in err
    assert "Execution Failed" not in err


def test_english_marker():
    assert extract_script_error(EN) == "Field 'X' not found"


def test_fallback_and_nothing():
    assert extract_script_error("a\nb\nScript error: bad thing\nExecution finished.") == "Script error: bad thing"
    assert extract_script_error("all good\nExecution finished.") is None
    assert extract_script_error("") is None
