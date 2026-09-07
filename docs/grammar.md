# MiniSQL 第一版文法

状态：协作规格，Parser 尚未实现。采用递归下降，必须与实现及测试同步更新。
关键字大小写不敏感；标识符使用 ASCII 字母/下划线开头，后接字母、数字或下划线。
字符串使用单引号，两个连续单引号表示一个单引号；支持 -- 行注释与 /* */ 非嵌套注释。

```ebnf
statement   = create | insert | select | delete ;
create      = "CREATE" "TABLE" identifier "(" column { "," column } ")" ";" ;
column      = identifier ( "INT" | "VARCHAR" ) ;
insert      = "INSERT" "INTO" identifier "(" identifier { "," identifier } ")"
              "VALUES" "(" literal { "," literal } ")" ";" ;
select      = "SELECT" ( "*" | identifier { "," identifier } )
              "FROM" identifier [ "WHERE" expression ] ";" ;
delete      = "DELETE" "FROM" identifier [ "WHERE" expression ] ";" ;
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
WHERE 必须为 BOOL；不支持连续比较 a < b < c，不支持 NULL、JOIN、UPDATE、浮点数或 VARCHAR(n)。
INSERT 必须列出全部表列，不允许重复，允许重排；缺分号必须报语法错误。
