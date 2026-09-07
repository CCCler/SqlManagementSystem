from minisql.contracts.plans import Plan


class Optimizer:
    def optimize(self, plan: Plan) -> Plan:
        raise NotImplementedError("成员一：常量折叠及布尔化简")
