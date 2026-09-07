from enum import Enum
from minisql.contracts.models import SourcePosition


class ErrorStage(str, Enum):
    LEXICAL = "lexical"
    SYNTAX = "syntax"
    SEMANTIC = "semantic"
    EXECUTION = "execution"
    STORAGE = "storage"


class MiniSQLError(Exception):
    """统一业务错误；I/O 错误允许无 SQL 位置。"""

    def __init__(self, stage: ErrorStage, code: str, reason: str,
                 position: SourcePosition | None = None,
                 expected: tuple[str, ...] = ()) -> None:
        self.stage = stage
        self.code = code
        self.reason = reason
        self.position = position
        self.expected = expected
        location = f" at {position.line}:{position.column}" if position else ""
        hint = f"; expected: {', '.join(expected)}" if expected else ""
        super().__init__(f"{stage.value}:{code}{location}: {reason}{hint}")
