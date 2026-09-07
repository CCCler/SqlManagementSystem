from minisql.contracts.interfaces import CatalogWriter, RecordStorage
from minisql.contracts.models import ExecutionResult
from minisql.contracts.plans import Plan


class PlanExecutor:
    def __init__(self, storage: RecordStorage, catalog: CatalogWriter) -> None:
        self.storage = storage
        self.catalog = catalog

    def execute(self, plan: Plan) -> ExecutionResult:
        raise NotImplementedError("成员三：执行 CreateTable/Insert/Scan/Filter/Project/Delete")
