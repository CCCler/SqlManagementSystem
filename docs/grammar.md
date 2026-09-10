# MiniSQL 第一版文法

状态：已实现递归下降解析，包含 DROP TABLE 与事务控制扩展；文法与实现及测试同步维护。
关键字大小写不敏感；标识符使用 ASCII 字母/下划线开头，后接字母、数字或下划线。
字符串使用单引号，两个连续单引号表示一个单引号；支持 -- 行注释与 /* */ 非嵌套注释。

```ebnf
statement   = create | insert | select | delete | update | drop | transaction | explain ;
transaction = ( "BEGIN" | "COMMIT" | "ROLLBACK" ) ";" ;
drop        = "DROP" "TABLE" identifier ";" ;
create      = "CREATE" "TABLE" identifier "(" column { "," column } ")" ";" ;
column      = identifier ( "INT" | "VARCHAR" ) ;
insert      = "INSERT" "INTO" identifier "(" identifier { "," identifier } ")"
              "VALUES" "(" literal { "," literal } ")" ";" ;
select      = "SELECT" [ "DISTINCT" ] ( "*" | identifier { "," identifier } )
              "FROM" identifier [ "WHERE" expression ]
              [ "ORDER" "BY" order_term { "," order_term } ]
              [ "LIMIT" integer [ "OFFSET" integer ] ] ";" ;
order_term  = identifier [ "ASC" | "DESC" ] ;
delete      = "DELETE" "FROM" identifier [ "WHERE" expression ] ";" ;
update      = "UPDATE" identifier "SET" assignment { "," assignment } [ "WHERE" expression ] ";" ;
assignment  = identifier "=" expression ;
explain     = "EXPLAIN" ( select | delete | update ) ;
expression  = or_expr ;
or_expr     = and_expr { "OR" and_expr } ;
and_expr    = not_expr { "AND" not_expr } ;
not_expr    = "NOT" not_expr | comparison ;
comparison  = additive [ comp_op additive ] ;
comp_op     = "=" | "!=" | "<>" | "<" | "<=" | ">" | ">=" ;
additive    = primary { ( "+" | "-" ) primary } ;
primary     = identifier | literal | "(" expression ")" ;
literal     = [ "-" ] integer | string | "TRUE" | "FALSE" ;
```

第一版采用专项 PPT 的 not_expr → comparison 结构：
NOT a = 1 解析成 NOT (a = 1)，优先级为算术加减 > 比较 > NOT > AND > OR。
专项 PPT 的文字优先级与其文法在 NOT/比较顺序上不一致，本项目以这里的明确文法和测试为准。

TRUE/FALSE 及整数加减用于验证布尔化简与常量折叠；不增加 BOOL 表列类型。
WHERE 必须为 BOOL；不支持连续比较 a < b < c，不支持 NULL、JOIN、浮点数或 VARCHAR(n)。
INSERT 必须列出全部表列，不允许重复，允许重排；缺分号必须报语法错误。

SELECT 可选 DISTINCT 去重、ORDER BY 排序与 LIMIT/OFFSET 截断；执行顺序为投影 → DISTINCT → ORDER BY → LIMIT/OFFSET。
ORDER BY 支持多列排序，逐列可选 ASC（默认）或 DESC，排序列必须出现在投影列中。
LIMIT 与 OFFSET 后必须是非负整数字面量；OFFSET 表示跳过前 N 行，只能跟在 LIMIT 之后，不能单独出现。
EXPLAIN 只编译不执行，仅渲染优化后的计划树；语义检查仍先执行，未知表/列与类型错误照常报告。
运行时加减运算结果超出有符号 64 位范围时报 EXECUTION:INTEGER_OUT_OF_RANGE，与语义分析及常量折叠一致。

DROP TABLE 删除表结构及全部记录，释放整表数据页；不存在的表报 UNKNOWN_TABLE，系统目录表报 PROTECTED_TABLE。
当前不支持 IF EXISTS、一次删除多表或 CASCADE。DROP 新增为保留关键字，不可再用作未加引号的标识符。

BEGIN、COMMIT、ROLLBACK 为保留关键字，不可用作标识符。事务控制同样要求分号，不支持 BEGIN TRANSACTION、嵌套事务或 SAVEPOINT。

实现限制：每条 WHERE 中左括号、NOT/AND/OR、比较及加减运算符总数最多 64（负数字面量的减号也计入）；字符串和注释内容不计入。第 65 个结构 Token 报带位置的 EXPRESSION_TOO_COMPLEX，限制在递归解析前检查。

布尔表达式保持执行器现有的左右依次求值、非短路语义。优化器仅在不会丢弃潜在溢出运算时使用 FALSE AND x / TRUE OR x 等吸收律；安全常量仍可折叠及生成 EmptyScan，不能通过优化隐藏运行时 INTEGER_OUT_OF_RANGE。

UPDATE 支持多列赋值，右侧使用现有表达式语法，类型必须与目标列一致，不支持隐式转换、重复赋值或 UPDATE 的 ORDER BY/LIMIT。所有赋值读取更新前的行，省略 WHERE 更新全部记录，影响行数按匹配行计（包含值未变化的行）。每个 SET 右侧表达式与 WHERE 各自最多 64 个结构 Token。UPDATE、SET 新增为保留关键字；旧数据库若使用这两个名称作为表名或列名，需要在升级前改用其他名称。EXPLAIN 内层语句与外层共用一个结尾分号。

UPDATE 复用堆存储的插入与删除，更新记录的 RecordId 和无 ORDER BY 时的扫描顺序可能改变；未更新记录的 RecordId 保持不变。变长记录允许迁移到其他页，文件格式不变。自动提交时失败恢复整条 UPDATE；显式事务失败后必须 ROLLBACK。
