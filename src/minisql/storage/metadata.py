"""系统表物理结构与元数据记录编解码（F07/F10/F11/F12，成员二契约）。

范围
----
本模块只定义"元数据怎么存"：系统表的固定列布局、编号识别规则，以及把账户盐/密钥、
联合列名等字段编码进记录行的辅助函数。不实现 Catalog 业务逻辑、SQL 语义与鉴权（成员三），
也不解析 SQL（成员一）；``__catalog`` 的表结构由 ``engine.catalog`` 单独持有。

方案 B（本模块采用，见 docs/SQL扩展元数据持久化约定-成员二.md）
--------------------------------------------------------------
- **同接口**：系统表就是普通表，走既有 ``create_table/insert/scan/delete/flush``，
  不新增存储接口、不改页格式。
- **动态编号**：``__catalog`` 固定占 ``table_id=0``，其余系统表由 ``next_table_id``
  动态分配并登记进 ``__catalog``，不预留固定号段，既有用户表编号无需重映射。
- **前缀识别**：表名以 ``__`` 开头即系统表；恢复时扫描 ``__catalog`` 中 ``__`` 前缀的
  行得到已存在编号，缺哪个建哪个（幂等）。
- **不升级格式**：所有列只用 INT/VARCHAR（含 NULL），V1 编解码器即可读写，格式版本仍为 V2。
"""
from __future__ import annotations

from minisql.contracts.models import ColumnSchema, DataType, TableSchema

SYSTEM_TABLE_PREFIX = "__"
SYSTEM_CATALOG_NAME = "__catalog"
# __catalog 固定占用 table_id=0；其余系统表编号由存储层动态分配。
SYSTEM_CATALOG_TABLE_ID = 0


def _int(name: str) -> ColumnSchema:
    return ColumnSchema(name, DataType.INT)


def _text(name: str) -> ColumnSchema:
    return ColumnSchema(name, DataType.VARCHAR)


# 列顺序即记录中的物理顺序，三方不得随意调整；新增列只能追加。
# 本模块是系统表结构的单一来源：engine/objects.py 直接引用这些常量，不得另行复制。
# root_page 为成员二对成员三提案的必需补充：索引根页号不持久化则无法重开恢复。
VIEWS = TableSchema("__views", (
    _int("view_id"), _text("view_name"), _text("definition"),
    _int("column_index"), _text("column_name"), _text("column_type"),
))

# 首版触发器固定 AFTER 行级，无需 timing 列；created_order 保证同事件多触发器顺序稳定。
TRIGGERS = TableSchema("__triggers", (
    _int("trigger_id"), _text("trigger_name"), _text("table_name"),
    _text("event"), _text("action"), _int("created_order"),
))

INDEXES = TableSchema("__indexes", (
    _int("index_id"), _text("index_name"), _text("table_name"), _int("unique_flag"),
    _int("column_index"), _text("column_name"), _int("root_page"),
))

USERS = TableSchema("__users", (
    _int("user_id"), _text("account_id"), _text("user_name"),
    _text("salt"), _text("key"), _int("iterations"), _int("is_admin"),
))

GRANTS = TableSchema("__grants", (
    _text("user_name"), _text("object_type"), _text("object_name"), _text("permission"),
))

DEPENDENCIES = TableSchema("__dependencies", (
    _text("object_type"), _text("object_name"),
    _text("depends_on_type"), _text("depends_on_name"),
))

# 现有 __catalog 只持久化 (table_id, table_name, column_index, column_name, column_type)，
# 不保存 nullable/default/约束；为避免改动其固定布局，约束另立此表。
CONSTRAINTS = TableSchema("__constraints", (
    _text("table_name"), _int("column_index"), _text("constraint_name"), _text("kind"),
    _text("expression"), _text("reference_table"), _text("reference_columns"),
    _text("default_text"),
))

# bootstrap 幂等补齐的顺序表；名称唯一，编号由存储层分配。
SYSTEM_TABLES: tuple[TableSchema, ...] = (
    VIEWS, TRIGGERS, INDEXES, USERS, GRANTS, DEPENDENCIES, CONSTRAINTS,
)

SYSTEM_TABLE_NAMES: tuple[str, ...] = tuple(schema.name for schema in SYSTEM_TABLES)


def is_system_table(name: str) -> bool:
    """系统表识别：名称以 ``__`` 开头（标识符大小写不敏感）。"""
    return name.lower().startswith(SYSTEM_TABLE_PREFIX)


# ---------- 记录层编码（只产出 VARCHAR/INT/NULL，不使用 repr/pickle） ----------

def encode_bytes(value: bytes) -> str:
    """二进制字段（账户 salt/key）编码为小写十六进制文本。"""
    return bytes(value).hex()


def decode_bytes(text: str) -> bytes:
    """还原 ``encode_bytes`` 的结果；非法十六进制由调用方按损坏数据报错。"""
    return bytes.fromhex(text)


def encode_name_list(names) -> str | None:
    """多列名编码：逗号分隔，顺序即键序；空列表记为 NULL（缺失不以空串表达）。"""
    names = tuple(names)
    return ",".join(names) if names else None


def decode_name_list(text: str | None) -> tuple[str, ...]:
    """还原 ``encode_name_list``；NULL 或空串都归一化为空元组。"""
    return tuple(text.split(",")) if text else ()
