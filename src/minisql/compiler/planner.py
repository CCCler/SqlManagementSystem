from minisql.contracts.plans import Plan, SemanticResult


class Planner:
    def build(self, semantic: SemanticResult) -> Plan:
        raise NotImplementedError("成员一：生成逻辑执行计划")
