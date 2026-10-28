ERROR_CODES = frozenset({
    "CONFIG_INVALID", "PAPER_ONLY", "STATE_INVALID", "STATE_MISSING", "STATE_IO",
    "CLOCK_REGRESSION", "PAUSE_INVALID", "SCHEMA_UNSUPPORTED", "MIGRATION_REQUIRED",
    "CONFIG_MISMATCH", "LOG_IO", "LOG_INVALID", "ARGUMENTS_INVALID", "PYTHON_UNSUPPORTED",
    "OPERATION_FAILED", "DATA_INVALID", "DATA_GAP", "DATA_TIMESTAMP", "DATA_CHECKSUM",
    "DATA_SNAPSHOT", "DATA_LOOKBACK", "DATA_STALE", "DATA_NETWORK", "NETWORK_OPT_IN_REQUIRED",
    "RISK_INVALID", "RISK_PROFILE_MISMATCH", "RISK_ID_CONFLICT", "RISK_TRANSITION", "RISK_HISTORY_FULL",
    "RESEARCH_INVALID", "RESEARCH_DATA", "RESEARCH_RECORD",
})


class MarvelError(Exception):
    """Only fixed, non-secret messages may cross the command-line boundary."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError("Unsupported public error code")
        super().__init__(message)
        self.code = code
        self.public_message = message


class ConfigError(MarvelError):
    pass


class StateError(MarvelError):
    pass


class LogError(MarvelError):
    pass


class DataError(MarvelError):
    pass


class RiskError(MarvelError):
    pass


class ResearchError(MarvelError):
    pass
