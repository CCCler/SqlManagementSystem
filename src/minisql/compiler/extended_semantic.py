"""只读绑定与计划生成；运行期行约束、鉴权和存储由后续模块接入。"""
from dataclasses import replace
from decimal import Decimal, localcontext
from minisql.contracts.extensions import (
    BoundStatement, Command, Constraint, Expr, ExtendedPlan, FieldBinding,
    JoinSource, ObjectDependency, OutputField, Query, TypeSpec, column_type,
)
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.compiler.extended_parser import AGGREGATES, ExtendedParser


def fail(code, reason, node=None):
    raise MiniSQLError(ErrorStage.SEMANTIC, code, reason, getattr(node, 'position', SourcePosition(1, 1)))


def common_type(a, b, node=None):
    nullable = a.nullable or b.nullable
    if a.kind == 'NULL':
        return replace(b, nullable=True)
    if b.kind == 'NULL':
        return replace(a, nullable=True)
    if a.kind == b.kind and a.kind != 'DECIMAL':
        return replace(a, nullable=nullable)
    if {a.kind, b.kind} <= {'INT', 'DECIMAL'}:
        p1, s1 = (19, 0) if a.kind == 'INT' else (a.precision, a.scale)
        p2, s2 = (19, 0) if b.kind == 'INT' else (b.precision, b.scale)
        scale = max(s1, s2)
        precision = max(p1-s1, p2-s2) + scale
        if precision > 38:
            fail('TYPE_MISMATCH', '共同小数类型超过 38 位', node)
        return TypeSpec('DECIMAL', precision, scale, nullable)
    fail('TYPE_MISMATCH', f'类型不兼容：{a.kind}/{b.kind}', node)


def numeric_type(op, a, b=None, node=None):
    b = b or a
    if a.kind not in ('INT', 'DECIMAL', 'NULL') or b.kind not in ('INT', 'DECIMAL', 'NULL'):
        fail('TYPE_MISMATCH', '算术和数值聚合要求 INT/DECIMAL', node)
    if a.kind == b.kind == 'INT' and op not in ('/', 'AVG'):
        return TypeSpec('INT', nullable=a.nullable or b.nullable)
    def parts(t):
        return (t.precision, t.scale) if t.kind == 'DECIMAL' else (19, 0)
    p1, s1 = parts(a)
    p2, s2 = parts(b)
    if op in ('/', 'AVG'):
        scale = max(6, s1+p2+1)
        integral = p1-s1+s2
    elif op == '*':
        scale, integral = s1+s2, p1+p2-s1-s2+1
    else:
        scale, integral = max(s1, s2), max(p1-s1, p2-s2)+1
    scale = min(scale, max(0, 38-integral))
    if op in ('/', 'AVG'):
        scale = max(6, scale)
    return TypeSpec('DECIMAL', min(38, integral+scale), scale, a.nullable or b.nullable)


def expr_key(expr):
    if expr.binding:
        b = expr.binding
        return ('field', b.scope, b.source, b.ordinal)
    return (expr.op, tuple(expr_key(a) if isinstance(a, Expr) else repr(a) for a in expr.args))


def has_aggregate(expr):
    return expr.op in AGGREGATES or any(has_aggregate(a) for a in expr.args if isinstance(a, Expr))


class Binder:
    def __init__(self, catalog):
        self.catalog = catalog
        self.scope_id = self.source_id = 0
        self.query_depth = 0
        self.expansion_nodes = 0
        self.views = []
        self.dependencies = set()
        self.special = []

    def budget(self, node=None):
        self.expansion_nodes += 1
        if self.expansion_nodes > 4096:
            fail('STATEMENT_TOO_COMPLEX', '展开后的编译节点超过 4096', node)

    def lookup(self, method, *args):
        reader = getattr(self.catalog, method, None)
        if reader is None:
            fail('CATALOG_CAPABILITY_UNAVAILABLE', f'目录尚未提供 {method}')
        return reader(*args)

    def dep(self, kind, name):
        self.dependencies.add((kind, name))

    def plan(self, op, children=(), expressions=(), output=(), attrs=(), caps=(), password=None):
        self.budget()
        dependencies = tuple(ObjectDependency(k, n) for k, n in sorted(self.dependencies))
        capabilities = set(caps)
        for child in children:
            if isinstance(child, ExtendedPlan):
                capabilities.update(child.capabilities)
        # 子查询表达式持有其计划，需要递归汇总能力。
        def visit(value):
            if isinstance(value, ExtendedPlan):
                capabilities.update(value.capabilities)
            elif isinstance(value, Expr):
                if value.op in ('scalar', 'EXISTS') or any(isinstance(a, ExtendedPlan) for a in value.args):
                    capabilities.add('subquery')
                for arg in value.args:
                    visit(arg)
        for expression in expressions:
            visit(expression)
        return ExtendedPlan(op, tuple(children), tuple(expressions), tuple(output), tuple(attrs), tuple(sorted(capabilities)), dependencies, password)

    def bind(self, ast):
        plan = self.query(ast, []) if isinstance(ast, Query) else self.command(ast)
        return BoundStatement(ast, plan)

    def table(self, name, node=None):
        schema = self.catalog.get_table(name)
        if schema is None:
            view_reader = getattr(self.catalog, 'get_view', None)
            if view_reader and view_reader(name) is not None:
                fail('READ_ONLY_VIEW', '视图不允许写入或修改表结构', node)
            fail('UNKNOWN_TABLE', f'未知表：{name}', node)
        if schema.table_id == 0 or name.startswith('__'):
            fail('PROTECTED_TABLE', '不能修改系统对象', node)
        self.dep('table', name)
        return schema

    def fields(self, schema, qualifier, scope):
        self.source_id += 1
        return [FieldBinding(scope, self.source_id, i, qualifier, c.name, column_type(c)) for i, c in enumerate(schema.columns)]

    def relation(self, rel, scope, outer=()):
        if isinstance(rel, JoinSource):
            left, lf = self.relation(rel.left, scope, outer)
            right, rf = self.relation(rel.right, scope, outer)
            if {f.qualifier for f in lf} & {f.qualifier for f in rf}:
                fail('DUPLICATE_ALIAS', '重复来源别名')
            on = self.expr(rel.on, [lf+rf]+list(outer)+self.special, False) if rel.on else None
            if on:
                self.boolean(on)
            if rel.kind == 'LEFT':
                rf = [replace(f, type=replace(f.type, nullable=True)) for f in rf]
            elif rel.kind == 'RIGHT':
                lf = [replace(f, type=replace(f.type, nullable=True)) for f in lf]
            output = [OutputField(f.name, f.type) for f in lf+rf]
            return self.plan('Join', (left, right), (on,) if on else (), output, (('kind', rel.kind),), ('join',)), lf+rf
        qualifier = rel.alias or rel.name
        if rel.query is not None:
            child = self.query(rel.query, [])
            self.source_id += 1
            fields = [FieldBinding(scope, self.source_id, i, qualifier, c.name, c.type) for i, c in enumerate(child.output)]
            self.unique([f.name for f in fields], 'DUPLICATE_COLUMN', rel)
            return self.plan('DerivedTable', (child,), output=child.output, attrs=(('alias', qualifier),), caps=('subquery',)), fields
        schema = self.catalog.get_table(rel.name)
        if schema is not None:
            self.dep('table', rel.name)
            fields = self.fields(schema, qualifier, scope)
            return self.plan('TableScan', output=[OutputField(f.name, f.type) for f in fields], attrs=(('table', rel.name), ('alias', qualifier))), fields
        if rel.name in self.views:
            fail('CYCLIC_VIEW', '视图循环依赖', rel)
        view = self.lookup('get_view', rel.name)
        if view is None:
            fail('UNKNOWN_TABLE', f'未知表或视图：{rel.name}', rel)
        if rel.name in self.views:
            fail('CYCLIC_VIEW', '视图循环依赖', rel)
        if len(self.views) >= 16:
            fail('QUERY_TOO_DEEP', '视图展开超过 16', rel)
        self.dep('view', rel.name)
        self.views.append(rel.name)
        try:
            q = view.query
            if isinstance(q, str):
                from minisql.compiler.lexer import Lexer
                q = ExtendedParser(Lexer().tokenize(q)).parse()
            if not isinstance(q, Query):
                fail('INVALID_VIEW', '视图定义必须为查询', rel)
            child = self.query(q, [])
        finally:
            self.views.pop()
        names = view.columns or tuple(c.name for c in child.output)
        if len(names) != len(child.output):
            fail('VALUE_COUNT_MISMATCH', '视图列数量不匹配', rel)
        self.unique(names, 'DUPLICATE_COLUMN', rel)
        self.source_id += 1
        fields = [FieldBinding(scope, self.source_id, i, qualifier, name, c.type) for i, (name, c) in enumerate(zip(names, child.output))]
        return self.plan('ViewScan', (child,), output=[OutputField(f.name, f.type) for f in fields], attrs=(('view', rel.name), ('alias', qualifier)), caps=('view',)), fields

    def resolve(self, expr, scopes):
        qualifier, name = expr.args
        for scope in scopes:
            matches = [f for f in scope if f.name == name and (qualifier is None or f.qualifier == qualifier)]
            if len(matches) > 1:
                fail('AMBIGUOUS_COLUMN', f'歧义字段：{name}', expr)
            if matches:
                return replace(expr, type=matches[0].type, binding=matches[0])
            if qualifier and any(f.qualifier == qualifier for f in scope):
                break  # 内层同名来源遮蔽外层。
        fail('UNKNOWN_COLUMN', f'未知字段：{qualifier + "." if qualifier else ""}{name}', expr)

    def boolean(self, expr):
        if expr.type.kind not in ('BOOL', 'NULL'):
            fail('TYPE_MISMATCH', '条件必须为 BOOL', expr)

    def expr(self, node, scopes, aggregates=False, inside_aggregate=False, subqueries=True):
        self.budget(node)
        op = node.op
        if op == 'column':
            return self.resolve(node, scopes)
        if op == 'literal':
            if node.type.kind == 'INT' and not -(2**63) <= node.args[0] < 2**63:
                fail('INTEGER_OUT_OF_RANGE', '整数超出 64 位范围', node)
            return node
        if op == 'star':
            fail('INVALID_EXPRESSION', '* 只能用于查询列表或 COUNT', node)
        if op in AGGREGATES:
            if not aggregates or inside_aggregate:
                fail('INVALID_AGGREGATE', '此处不允许聚合或嵌套聚合', node)
            args = tuple(self.expr(a, scopes, True, True, False) for a in node.args)
            if op == 'COUNT':
                typ = TypeSpec('INT', nullable=False)
            elif op in ('SUM', 'AVG'):
                typ = replace(numeric_type(op, args[0].type, node=node), nullable=True)
            else:
                typ = replace(args[0].type, nullable=True)
            return replace(node, args=args, type=typ)
        if op in ('scalar', 'EXISTS') or op == 'IN' and any(isinstance(a, Query) for a in node.args):
            if not subqueries:
                fail('INVALID_SUBQUERY', '此处不允许子查询', node)
            q = node.args[-1]
            plan = self.query(q, scopes)
            if op != 'EXISTS' and len(plan.output) != 1:
                fail('SUBQUERY_COLUMN_COUNT', '子查询必须只返回一列', node)
            if op == 'IN':
                left = self.expr(node.args[0], scopes, aggregates, inside_aggregate, subqueries)
                common_type(left.type, plan.output[0].type, node)
                args = (left, plan)
            else:
                args = (plan,)
            typ = replace(plan.output[0].type, nullable=True) if op == 'scalar' else TypeSpec('BOOL', nullable=op != 'EXISTS')
            return replace(node, args=args, type=typ)
        args = tuple(self.expr(a, scopes, aggregates, inside_aggregate, subqueries) for a in node.args)
        nullable = any(a.type.nullable or a.type.kind == 'NULL' for a in args)
        typ = TypeSpec('BOOL', nullable=nullable)
        if op in ('+', '-', '*', '/'):
            typ = numeric_type(op, args[0].type, args[1].type, node)
        elif op in ('AND', 'OR', 'NOT'):
            for arg in args:
                self.boolean(arg)
        elif op in ('=', '!=', '<>', '<', '<=', '>', '>=', 'BETWEEN', 'IN'):
            for arg in args[1:]:
                common_type(args[0].type, arg.type, node)
        elif op in ('IS NULL', 'IS NOT NULL'):
            typ = TypeSpec('BOOL', nullable=False)
        elif op == 'LIKE':
            if any(a.type.kind not in ('VARCHAR', 'NULL') for a in args):
                fail('TYPE_MISMATCH', 'LIKE 要求字符串', node)
            if len(args) == 3 and (args[2].op != 'literal' or not isinstance(args[2].args[0], str) or len(args[2].args[0]) != 1):
                fail('INVALID_ESCAPE', 'ESCAPE 要求单字符常量', node)
        else:
            fail('INVALID_EXPRESSION', f'未知表达式：{op}', node)
        return replace(node, args=args, type=typ)

    def query(self, q, outer):
        self.query_depth += 1
        if self.query_depth > 16:
            fail('QUERY_TOO_DEEP', '查询或视图展开超过 16', q)
        try:
            return self._query(q, outer)
        finally:
            self.query_depth -= 1

    def _query(self, q, outer):
        if q.set_op:
            left, right = self.query(q.left, outer), self.query(q.right, outer)
            if len(left.output) != len(right.output):
                fail('SET_COLUMN_COUNT', '集合操作列数不一致', q)
            output = tuple(OutputField(a.name, common_type(a.type, b.type, q)) for a, b in zip(left.output, right.output))
            plan = self.plan('SetOperation', (left, right), output=output, attrs=(('kind', q.set_op),), caps=('set_operation',))
            self.scope_id += 1
            fields = [FieldBinding(self.scope_id, 0, i, '', c.name, c.type) for i, c in enumerate(output)]
            return self.sort_limit(plan, q, [fields], {}, (), False)
        self.scope_id += 1
        scope = self.scope_id
        source, fields = self.relation(q.source, scope, outer)
        scopes = [fields] + outer + self.special
        where = self.expr(q.where, scopes) if q.where else None
        if where:
            self.boolean(where)
            source = self.plan('Filter', (source,), (where,), source.output)
        groups = tuple(self.expr(e, scopes) for e in q.group_by)
        items = []
        aliases = {}
        for item in q.items:
            expr = item.expression
            if expr.op == 'star':
                if item.alias:
                    fail('INVALID_ALIAS', '* 不允许列别名', expr)
                selected = [f for f in fields if not expr.args or f.qualifier == expr.args[0]]
                if not selected:
                    fail('UNKNOWN_COLUMN', '未知来源的 *', expr)
                items.extend((f.name, Expr('column', (f.qualifier, f.name), expr.position, f.type, f)) for f in selected)
            else:
                bound = self.expr(expr, scopes, True)
                name = item.alias or (expr.args[-1] if expr.op == 'column' else f'expr_{len(items)+1}')
                items.append((name, bound))
                if item.alias:
                    aliases.setdefault(item.alias, []).append(bound)
        having = self.expr(q.having, scopes, True) if q.having else None
        if having:
            self.boolean(having)
        grouped = bool(groups or having or any(has_aggregate(e) for _, e in items) or any(has_aggregate(e) for e, _ in q.order_by))
        if grouped:
            keys = {expr_key(e) for e in groups}
            def validate(expr):
                if expr.op in AGGREGATES or expr_key(expr) in keys:
                    return
                if expr.binding and expr.binding.scope == scope:
                    fail('NON_GROUPED_COLUMN', '输出或 HAVING 使用非分组字段', expr)
                for arg in expr.args:
                    if isinstance(arg, Expr):
                        validate(arg)
                    elif isinstance(arg, ExtendedPlan):
                        for dependency in self.plan_expressions(arg):
                            validate(dependency)
            for _, expr in items:
                validate(expr)
            if having:
                validate(having)
            source = self.plan('Aggregate', (source,), groups + tuple(e for _, e in items), attrs=(('group_count', len(groups)),), caps=('aggregate',))
            if having:
                source = self.plan('Having', (source,), (having,))
        output = tuple(OutputField(n, e.type) for n, e in items)
        plan = self.plan('ExpressionProject', (source,), [e for _, e in items], output, caps=('extended_expression',))
        if q.distinct:
            plan = self.plan('Distinct', (plan,), output=output)
        # ORDER BY 可引用输出别名；分组检查复用投影检查。
        result = self.sort_limit(plan, q, scopes, aliases, tuple(e for _, e in items), grouped)
        if grouped and result.operator in ('Sort', 'Limit'):
            sort = result.children[0] if result.operator == 'Limit' else result
            if sort.operator == 'Sort':
                for expr in sort.expressions:
                    validate(expr)
        if grouped:
            aggregates = {}
            def collect(expr):
                if expr.op in AGGREGATES:
                    aggregates.setdefault(expr_key(expr), expr)
                for arg in expr.args:
                    if isinstance(arg, Expr):
                        collect(arg)
            # 收集投影、HAVING、ORDER BY 中的聚合，不进入不同作用域的子查询。
            for _, expr in items:
                collect(expr)
            if having:
                collect(having)
            sort = result.children[0] if result.operator == 'Limit' else result
            if sort.operator == 'Sort':
                for expr in sort.expressions:
                    collect(expr)
            def install(plan):
                if plan.operator == 'Aggregate':
                    return replace(plan, attributes=plan.attributes+(('aggregates', tuple(aggregates.values())),))
                return replace(plan, children=tuple(install(c) for c in plan.children))
            result = install(result)
        return result

    def plan_expressions(self, plan):
        yield from plan.expressions
        for child in plan.children:
            if isinstance(child, ExtendedPlan):
                yield from self.plan_expressions(child)

    def sort_limit(self, plan, q, scopes, aliases, projected, grouped):
        orders = []
        for raw, desc in q.order_by:
            alias = raw.args[-1] if raw.op == 'column' and raw.args[0] is None else None
            if alias in aliases:
                if len(aliases[alias]) != 1:
                    fail('AMBIGUOUS_COLUMN', '排序别名不唯一', raw)
                expr = aliases[alias][0]
            else:
                expr = self.expr(raw, scopes, grouped)
            if q.distinct and expr_key(expr) not in {expr_key(e) for e in projected}:
                fail('INVALID_ORDER_BY', 'DISTINCT 排序表达式必须在输出中', raw)
            orders.append(expr)
        if orders:
            plan = self.plan('Sort', (plan,), orders, plan.output, (('descending', tuple(d for _, d in q.order_by)), ('nulls', 'ASC LAST / DESC FIRST')))
        if q.limit is not None:
            plan = self.plan('Limit', (plan,), output=plan.output, attrs=(('limit', q.limit), ('offset', q.offset or 0)))
        return plan

    def unique(self, names, code, node=None):
        if len(names) != len(set(names)):
            fail(code, '重复名称或列', node)

    def check_assignment(self, column, expr):
        target = column_type(column)
        if expr.type.kind == 'NULL':
            if not target.nullable:
                fail('NOT_NULL_VIOLATION', '非空列不能赋 NULL', expr)
            return
        if target.kind != expr.type.kind and not (target.kind == 'DECIMAL' and expr.type.kind == 'INT'):
            fail('TYPE_MISMATCH', f'列 {column.name} 类型不匹配', expr)
        if target.kind == 'DECIMAL' and expr.op == 'literal':
            with localcontext() as ctx:
                ctx.prec = 100
                value = Decimal(expr.args[0])
                quantum = Decimal(1).scaleb(-target.scale)
                if value != value.quantize(quantum) or abs(value) >= Decimal(10)**(target.precision-target.scale):
                    fail('NUMERIC_OUT_OF_RANGE', '小数赋值超出精度或丢失非零小数', expr)

    def check_schema(self, schema, node):
        self.unique([c.name for c in schema.columns], 'DUPLICATE_COLUMN', node)
        if not schema.columns:
            fail('INVALID_SCHEMA', '表至少保留一列', node)
        columns = {c.name: c for c in schema.columns}
        primary = [c for c in schema.constraints if c.kind == 'PRIMARY KEY']
        if len(primary) > 1:
            fail('DUPLICATE_CONSTRAINT', '只能有一个主键', node)
        if primary:
            schema = replace(schema, columns=tuple(replace(c, nullable=False) if c.name in primary[0].columns else c for c in schema.columns))
            columns = {c.name: c for c in schema.columns}
        self.scope_id += 1
        scopes = [self.fields(schema, schema.name, self.scope_id)]
        self.unique([c.name for c in schema.constraints if c.name], 'DUPLICATE_CONSTRAINT', node)
        constraints = []
        for c in schema.constraints:
            self.unique(c.columns, 'DUPLICATE_COLUMN', node)
            if any(n not in columns for n in c.columns):
                fail('UNKNOWN_COLUMN', '约束引用未知列', node)
            if c.kind == 'FOREIGN KEY':
                ref = schema if c.reference_table == schema.name else self.table(c.reference_table, node)
                ref_columns = {x.name: x for x in ref.columns}
                if len(c.columns) != len(c.reference_columns) or any(n not in ref_columns for n in c.reference_columns):
                    fail('INVALID_FOREIGN_KEY', '外键列数或引用列不合法', node)
                if not any(x.kind in ('PRIMARY KEY', 'UNIQUE') and x.columns == c.reference_columns for x in ref.constraints):
                    fail('INVALID_FOREIGN_KEY', '外键必须引用主键或唯一键', node)
                for a, b in zip(c.columns, c.reference_columns):
                    if replace(column_type(columns[a]), nullable=True) != replace(column_type(ref_columns[b]), nullable=True):
                        fail('TYPE_MISMATCH', '外键类型不一致', node)
            if c.expression:
                expr = self.expr(c.expression, scopes, subqueries=False)
                self.boolean(expr)
                c = replace(c, expression=expr)
            constraints.append(c)
        bound_columns = []
        for column in schema.columns:
            if column.has_default:
                expr = self.expr(column.default, [], subqueries=False)
                from minisql.compiler.extended_optimizer import fold_expr
                expr = fold_expr(expr)
                self.check_assignment(column, expr)
                column = replace(column, default=expr)
            bound_columns.append(column)
        return replace(schema, columns=tuple(bound_columns), constraints=tuple(constraints))

    def command(self, cmd):
        kind, name, data = cmd.kind, cmd.name, cmd.payload
        if kind == 'EXPLAIN':
            inner = self.bind(data[0]).plan
            return self.plan('Explain', (inner,), caps=())
        if name.startswith('__'):
            fail('PROTECTED_TABLE', '不能管理系统对象', cmd)
        if kind in ('INSERT', 'UPDATE', 'DELETE'):
            schema = self.table(name, cmd)
            self.scope_id += 1
            scopes = [self.fields(schema, name, self.scope_id)] + self.special
            columns = {c.name: c for c in schema.columns}
            values = []
            if kind == 'INSERT':
                names, raw = data
                self.unique(names, 'DUPLICATE_COLUMN', cmd)
                if len(names) != len(raw):
                    fail('VALUE_COUNT_MISMATCH', '列数和值数量不匹配', cmd)
                if set(names) != set(columns):
                    fail('MISSING_COLUMN', 'INSERT 必须提供全部表列', cmd)
                assignments = zip(names, raw)
                where = None
            else:
                assignments, where = data
                self.unique([n for n, _ in assignments], 'DUPLICATE_COLUMN', cmd)
            for n, raw in assignments:
                if n not in columns:
                    fail('UNKNOWN_COLUMN', f'未知列：{n}', cmd)
                expr = self.expr(raw, self.special if kind == 'INSERT' else scopes)
                self.check_assignment(columns[n], expr)
                values.append((n, expr))
            predicate = self.expr(where, scopes) if where else None
            if predicate:
                self.boolean(predicate)
            return self.plan(kind.title(), expressions=[v for _, v in values]+([predicate] if predicate else []), attrs=(('table', name), ('assignments', tuple(n for n, _ in values)), ('has_where', predicate is not None)), caps=('extended_dml',))
        if kind == 'CREATE TABLE':
            if self.catalog.get_table(name) is not None:
                fail('DUPLICATE_TABLE', f'表已存在：{name}', cmd)
            reader = getattr(self.catalog, 'get_view', None)
            if reader and reader(name):
                fail('DUPLICATE_TABLE', '与视图重名', cmd)
            schema = self.check_schema(data[0], cmd)
            return self.plan('CreateTable', attrs=(('schema', schema),), caps=('extended_schema',))
        if kind == 'ALTER TABLE':
            schema = self.table(name, cmd)
            action = data[0]
            columns = {c.name: c for c in schema.columns}
            if action == 'ADD COLUMN':
                schema = replace(schema, columns=schema.columns+(data[1],), constraints=schema.constraints+data[2])
            elif action == 'ADD CONSTRAINT':
                if data[1].name is None:
                    fail('INVALID_CONSTRAINT', 'ADD CONSTRAINT 必须命名', cmd)
                schema = replace(schema, constraints=schema.constraints+(data[1],))
            elif action == 'DROP CONSTRAINT':
                if not any(c.name == data[1] for c in schema.constraints):
                    fail('UNKNOWN_CONSTRAINT', '约束不存在', cmd)
                schema = replace(schema, constraints=tuple(c for c in schema.constraints if c.name != data[1]))
            elif action == 'RENAME TABLE':
                if self.catalog.get_table(data[1]) or self.lookup('get_view', data[1]):
                    fail('DUPLICATE_TABLE', '目标名称已存在', cmd)
                schema = replace(schema, name=data[1])
            else:
                if data[1] not in columns:
                    fail('UNKNOWN_COLUMN', data[1], cmd)
                if action == 'DROP COLUMN':
                    if any(data[1] in c.columns or c.expression for c in schema.constraints):
                        fail('DEPENDENT_OBJECT', '列被约束引用，先处理依赖', cmd)
                    schema = replace(schema, columns=tuple(c for c in schema.columns if c.name != data[1]))
                elif action == 'RENAME COLUMN':
                    if any(data[1] in c.columns or c.expression for c in schema.constraints):
                        fail('DEPENDENT_OBJECT', '列被约束引用，先处理依赖', cmd)
                    schema = replace(schema, columns=tuple(replace(c, name=data[2]) if c.name == data[1] else c for c in schema.columns))
                else:
                    typ = data[2]
                    schema = replace(schema, columns=tuple(replace(c, data_type=DataType(typ.kind), precision=typ.precision, scale=typ.scale) if c.name == data[1] else c for c in schema.columns))
            if self.lookup('get_dependencies', 'table', name):
                fail('DEPENDENT_OBJECT', '表存在依赖对象', cmd)
            schema = self.check_schema(schema, cmd)
            return self.plan('AlterTable', attrs=(('table', name), ('action', action), ('schema', schema)), caps=('alter_table',))
        if kind == 'CREATE INDEX':
            table, columns, unique = data
            schema = self.table(table, cmd)
            if self.lookup('get_index', name):
                fail('DUPLICATE_INDEX', '索引已存在', cmd)
            self.unique(columns, 'DUPLICATE_COLUMN', cmd)
            if any(n not in {c.name for c in schema.columns} for n in columns):
                fail('UNKNOWN_COLUMN', '索引列不存在', cmd)
            return self.plan('CreateIndex', attrs=(('index', name), ('table', table), ('columns', columns), ('unique', unique)), caps=('index_ddl',))
        if kind == 'CREATE VIEW':
            if self.catalog.get_table(name) or self.lookup('get_view', name):
                fail('DUPLICATE_VIEW', '对象已存在', cmd)
            self.views.append(name)
            try:
                child = self.query(data[1], [])
            finally:
                self.views.pop()
            names = data[0] or tuple(f.name for f in child.output)
            if len(names) != len(child.output):
                fail('VALUE_COUNT_MISMATCH', '视图列数不匹配', cmd)
            self.unique(names, 'DUPLICATE_COLUMN', cmd)
            output = tuple(OutputField(n, f.type) for n, f in zip(names, child.output))
            return self.plan('CreateView', (child,), output=output, attrs=(('view', name),), caps=('view_ddl',))
        if kind == 'CREATE TRIGGER':
            table, event, actions = data
            schema = self.table(table, cmd)
            if self.lookup('get_trigger', name):
                fail('DUPLICATE_TRIGGER', '触发器已存在', cmd)
            self.scope_id += 1
            aliases = ('new',) if event == 'INSERT' else ('old',) if event == 'DELETE' else ('old', 'new')
            special = [f for alias in aliases for f in self.fields(schema, alias, self.scope_id)]
            previous, self.special = self.special, [special]
            try:
                children = tuple(self.bind(a).plan for a in actions)
            finally:
                self.special = previous
            writes = tuple((a.name, a.kind) for a in actions if isinstance(a, Command))
            graph = {}
            for trigger in self.lookup('list_triggers'):
                graph.setdefault((trigger.table, trigger.event), set()).update(trigger.writes)
            graph.setdefault((table, event), set()).update(writes)
            def cyclic(start, path):
                if start in path:
                    return True
                return any(cyclic(n, path | {start}) for n in graph.get(start, ()))
            if cyclic((table, event), set()):
                fail('RECURSIVE_TRIGGER', '触发器直接或间接递归', cmd)
            return self.plan('CreateTrigger', children, attrs=(('trigger', name), ('table', table), ('event', event), ('writes', writes), ('discard_select_results', True)), caps=('trigger_ddl',))
        if kind in ('CREATE USER', 'ALTER USER', 'DROP USER'):
            account = self.lookup('get_account', name)
            if kind == 'CREATE USER' and account is not None:
                fail('DUPLICATE_USER', '账户已存在', cmd)
            if kind != 'CREATE USER' and account is None:
                fail('UNKNOWN_USER', '账户不存在', cmd)
            return self.plan(kind.title().replace(' ', ''), attrs=(('user', name),), caps=('user_management',), password=cmd.password)
        if kind in ('GRANT', 'REVOKE'):
            permissions, obj, user = data
            self.unique(permissions, 'DUPLICATE_PERMISSION', cmd)
            if self.lookup('get_account', user) is None:
                fail('UNKNOWN_USER', '账户不存在', cmd)
            valid = {'database': {'CREATE TABLE', 'CREATE INDEX', 'CREATE VIEW', 'CREATE TRIGGER'}, 'table': {'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER'}, 'view': {'SELECT', 'DROP'}, 'index': {'DROP'}, 'trigger': {'DROP'}}
            if any(p not in valid.get(obj, ()) for p in permissions):
                fail('UNKNOWN_PERMISSION', '权限与对象不匹配', cmd)
            if obj == 'database':
                if not self.lookup('has_database', name):
                    fail('UNKNOWN_DATABASE', '数据库不存在', cmd)
            elif obj == 'table':
                self.table(name, cmd)
            elif self.lookup('get_' + obj, name) is None:
                fail('UNKNOWN_OBJECT', '授权对象不存在', cmd)
            return self.plan(kind.title(), attrs=(('object_kind', obj), ('object', name), ('user', user), ('permissions', permissions)), caps=('authorization',))
        if kind.startswith('DROP '):
            obj = kind.split()[1].lower()
            if obj == 'table':
                self.table(name, cmd)
            elif self.lookup('get_' + obj, name) is None:
                fail('UNKNOWN_OBJECT', '对象不存在', cmd)
            if self.lookup('get_dependencies', obj, name):
                fail('DEPENDENT_OBJECT', '对象存在依赖', cmd)
            return self.plan(kind.title().replace(' ', ''), attrs=(('name', name),), caps=(obj+'_ddl',))
        fail('UNKNOWN_PLAN', '未知扩展语句', cmd)
