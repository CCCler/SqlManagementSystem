"""现有目录的只读适配：缺失扩展能力保持缺失，不伪造空对象集合。"""
class ReadOnlyCatalogAdapter:
    METHODS = frozenset(('get_table', 'list_tables', 'get_view', 'get_index',
                         'list_indexes', 'get_trigger', 'list_triggers',
                         'get_account', 'get_dependencies', 'has_database'))

    def __init__(self, catalog):
        self._catalog = catalog
        self.positions = {}

    def get_table(self, name):
        if name.lower().startswith('__'):
            from minisql.contracts.errors import MiniSQLError, ErrorStage
            raise MiniSQLError(ErrorStage.SEMANTIC, 'PROTECTED_TABLE', 'SQL 不允许访问内部系统表', self.positions.get(name.lower()))
        return self._catalog.get_table(name)

    def __getattr__(self, name):
        if name in self.METHODS:
            return getattr(self._catalog, name)
        raise AttributeError(name)
