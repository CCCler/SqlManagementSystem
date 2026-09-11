"""现有目录的只读适配：缺失扩展能力保持缺失，不伪造空对象集合。"""
class ReadOnlyCatalogAdapter:
    METHODS = frozenset(('get_table', 'list_tables', 'get_view', 'get_index',
                         'list_indexes', 'get_trigger', 'list_triggers',
                         'get_account', 'get_dependencies', 'has_database'))

    def __init__(self, catalog):
        self._catalog = catalog

    def __getattr__(self, name):
        if name in self.METHODS:
            return getattr(self._catalog, name)
        raise AttributeError(name)
