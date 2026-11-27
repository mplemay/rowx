# Changelog

Each release lists features, fixes, performance work, and breaking changes.

## [0.5.0](https://github.com/mplemay/rowx/compare/v0.4.2...v0.5.0) - 2026-11-27

### Features

- Infer sqlite nested statement nullability and type from bytecode ([#394](https://github.com/mplemay/rowx/pull/394))
- Infer sqlite computed column nullability and type from bytecode ([#393](https://github.com/mplemay/rowx/pull/393))
- Add the sqlite provisioner for rowx.testing ([#389](https://github.com/mplemay/rowx/pull/389))
- Add sqlite database administration and scratch checks ([#385](https://github.com/mplemay/rowx/pull/385))
- Add the sqlite checker backend ([#384](https://github.com/mplemay/rowx/pull/384))
- Add sqlite migrations ([#381](https://github.com/mplemay/rowx/pull/381))
- Add the sqlite pool ([#379](https://github.com/mplemay/rowx/pull/379))
- Add the sqlite describe and checker connection ([#377](https://github.com/mplemay/rowx/pull/377))
- Add the sqlite connection, transaction and stream bindings ([#376](https://github.com/mplemay/rowx/pull/376))
- Add the sqlite connection worker, execution, cancellation and statement timeout
  ([#369](https://github.com/mplemay/rowx/pull/369))
- Add the sqlite compiler and lexer ([#368](https://github.com/mplemay/rowx/pull/368))
- Add sqlite dsn, error mapping, type table and value codecs ([#367](https://github.com/mplemay/rowx/pull/367))
- Scaffold the rowx-sqlite crate and package ([#366](https://github.com/mplemay/rowx/pull/366))

### Bug fixes

- Wrap the release changelog with the project rumdl config ([#403](https://github.com/mplemay/rowx/pull/403))
- Undo a transaction whose begin was cancelled after it opened ([#398](https://github.com/mplemay/rowx/pull/398))
- Pin relative sqlite paths, cap parameters and harden codecs ([#397](https://github.com/mplemay/rowx/pull/397))
- Harden sqlite migrations, wal switch and database drop ([#396](https://github.com/mplemay/rowx/pull/396))
- Type sqlite compound selects by every arm and document bytecode inference
  ([#395](https://github.com/mplemay/rowx/pull/395))
- Accept the factored embedded extra marker ([#359](https://github.com/mplemay/rowx/pull/359))

## [0.4.2](https://github.com/mplemay/rowx/compare/v0.3.0...v0.4.2) - 2026-11-16

### Features

- Add embedded postgres runtime ([#316](https://github.com/mplemay/rowx/pull/316))

### Bug fixes

- Drop the deprecated pep 639 license classifier ([#329](https://github.com/mplemay/rowx/pull/329))

## [0.3.0](https://github.com/mplemay/rowx/compare/v0.2.0...v0.3.0) - 2026-11-12

### Features

- Re-export the rowx_core names that rowx does not already export ([#324](https://github.com/mplemay/rowx/pull/324))
- Use-rowx agent skill ([#315](https://github.com/mplemay/rowx/pull/315))
- Add checker suppressions and rule configuration ([#310](https://github.com/mplemay/rowx/pull/310))
- Check query compatibility across migration revisions ([#292](https://github.com/mplemay/rowx/pull/292))
- Show the described contract for selected query sites ([#291](https://github.com/mplemay/rowx/pull/291))
- Prove non-null projections from direct postgres scan filters ([#289](https://github.com/mplemay/rowx/pull/289))
- Report and enforce query check coverage in ci ([#288](https://github.com/mplemay/rowx/pull/288))

### Bug fixes

- Keep the SCRAM client reply when the test peer resets ([#294](https://github.com/mplemay/rowx/pull/294))
- Fail closed on wheel metadata drift ([#290](https://github.com/mplemay/rowx/pull/290))

## [0.2.0](https://github.com/mplemay/rowx/releases/tag/v0.2.0) - 2026-11-05

### Features

- Typed channel_binding and auth/tls downgrade tests
- Add the template-construction checker rule
- Resolve string.templatelib names in the checker
- Watch session encoding and mark drifted connections broken
- Binary ranges and remove text literals
- Binary element encoders and binary arrays
- Generic row classes and generic aliases ([#211](https://github.com/mplemay/rowx/pull/211))
- Check pydantic row types ([#209](https://github.com/mplemay/rowx/pull/209))
- Check dataclass, NamedTuple and TypedDict row types ([#207](https://github.com/mplemay/rowx/pull/207))
- Add pydantic row plans ([#206](https://github.com/mplemay/rowx/pull/206))
- Add named row plans for dataclass, NamedTuple and TypedDict ([#205](https://github.com/mplemay/rowx/pull/205))
- Build named rows in the binding ([#204](https://github.com/mplemay/rowx/pull/204))
- Add column-to-field mapping to rowx-core and rowx-postgres ([#193](https://github.com/mplemay/rowx/pull/193))
- Migrate skip, info json, connect timeout and policy config ([#178](https://github.com/mplemay/rowx/pull/178))
- Migrator revert, results and one migration per version ([#177](https://github.com/mplemay/rowx/pull/177))
- Add rowx.testing pytest plugin ([#164](https://github.com/mplemay/rowx/pull/164))
- Add rowx check --scratch ([#163](https://github.com/mplemay/rowx/pull/163))
- Add migration drift gate to rowx check ([#162](https://github.com/mplemay/rowx/pull/162))
- Add rowx database command ([#161](https://github.com/mplemay/rowx/pull/161))
- Add rowx migrate command ([#160](https://github.com/mplemay/rowx/pull/160))
- Add rowx.postgres.migrator ([#159](https://github.com/mplemay/rowx/pull/159))
- Add postgres migration executor ([#158](https://github.com/mplemay/rowx/pull/158))
- Add postgres database admin operations ([#155](https://github.com/mplemay/rowx/pull/155))
- Add migration source resolution and plan to rowx-core ([#154](https://github.com/mplemay/rowx/pull/154))
- Mark never-null computed columns as not null ([#151](https://github.com/mplemay/rowx/pull/151))
- Infer set operation column nullability from the plan ([#138](https://github.com/mplemay/rowx/pull/138))
- Infer time, bytearray and memoryview params ([#136](https://github.com/mplemay/rowx/pull/136))
- Infer view column nullability from the view definition ([#130](https://github.com/mplemay/rowx/pull/130))
- Map scan-boundary columns to their source ([#128](https://github.com/mplemay/rowx/pull/128))
- Add hstore, ltree, lquery and cube ([#127](https://github.com/mplemay/rowx/pull/127))
- Add geometric types ([#126](https://github.com/mplemay/rowx/pull/126))
- Add money, bit strings and intervals with months ([#125](https://github.com/mplemay/rowx/pull/125))
- Add range types ([#124](https://github.com/mplemay/rowx/pull/124))
- Add inet, cidr and macaddr types ([#122](https://github.com/mplemay/rowx/pull/122))
- Add asyncpg and psycopg sqlalchemy fetch benchmark ([#91](https://github.com/mplemay/rowx/pull/91))
- Add pool.transaction ([#90](https://github.com/mplemay/rowx/pull/90))
- Add transactions and savepoints ([#89](https://github.com/mplemay/rowx/pull/89))
- Add rowx.postgres pool ([#88](https://github.com/mplemay/rowx/pull/88))
- Add connection pool to rowx-core ([#87](https://github.com/mplemay/rowx/pull/87))
- Add connectoptions, ping and reset ([#85](https://github.com/mplemay/rowx/pull/85))
- Add streaming fetch returning rowstream ([#69](https://github.com/mplemay/rowx/pull/69))
- Split rowx-pg exec into open and streaming consumers ([#68](https://github.com/mplemay/rowx/pull/68))
- Add one, optional, all and query result ([#67](https://github.com/mplemay/rowx/pull/67))
- Add release workflow and wheel smoke test (t47) ([#57](https://github.com/mplemay/rowx/pull/57))
- Add example app checked in ci (t46) ([#56](https://github.com/mplemay/rowx/pull/56))
- Add param-type-mismatch wiring (t44) ([#54](https://github.com/mplemay/rowx/pull/54))
- Add parameter type inference (t43) ([#53](https://github.com/mplemay/rowx/pull/53))
- Add rule fixture suites and fidelity test (t42) ([#49](https://github.com/mplemay/rowx/pull/49))
- Add cli orchestration and fixture harness (t41) ([#48](https://github.com/mplemay/rowx/pull/48))
- Add diagnostic rendering (t40) ([#47](https://github.com/mplemay/rowx/pull/47))
- Add comparison engine (t39) ([#46](https://github.com/mplemay/rowx/pull/46))
- Add row-type resolution (t38) ([#44](https://github.com/mplemay/rowx/pull/44))
- Add source map (t37) ([#42](https://github.com/mplemay/rowx/pull/42))
- Add discovery with lexical resolution and call shape (t36) ([#41](https://github.com/mplemay/rowx/pull/41))
- Add checker config and file walking (t35) ([#39](https://github.com/mplemay/rowx/pull/39))
- Integrate explain, overrides and defaulting into describe (t33) ([#37](https://github.com/mplemay/rowx/pull/37))
- Add base nullability from the catalog (t32) ([#36](https://github.com/mplemay/rowx/pull/36))
- Add describe base and checker session (t31) ([#35](https://github.com/mplemay/rowx/pull/35))
- Add explain plan walker scan boundaries (t30) ([#33](https://github.com/mplemay/rowx/pull/33))
- Add explain plan walker join marking (t29) ([#32](https://github.com/mplemay/rowx/pull/32))
- Add explain fixture capture (t28) ([#31](https://github.com/mplemay/rowx/pull/31))
- Add runtime half of conformance suite (t27) ([#30](https://github.com/mplemay/rowx/pull/30))
- Add type-checker suite (t26) ([#29](https://github.com/mplemay/rowx/pull/29))
- Add end-to-end example and runtime error suite (t25) ([#28](https://github.com/mplemay/rowx/pull/28))
- Add fetch_100k benchmark against asyncpg (t24) ([#27](https://github.com/mplemay/rowx/pull/27))
- Add tls with rustls and sslmode mapping (t23) ([#26](https://github.com/mplemay/rowx/pull/26))
- Add cancellation with settling and own cancel request (t22) ([#25](https://github.com/mplemay/rowx/pull/25))
- Add connection state machine (t21) ([#24](https://github.com/mplemay/rowx/pull/24))
- Add statement cache and retry rules (t20) ([#23](https://github.com/mplemay/rowx/pull/23))
- Add codec completeness gate (t19) ([#22](https://github.com/mplemay/rowx/pull/22))
- Add array encode codecs (t18) ([#21](https://github.com/mplemay/rowx/pull/21))
- Add array decode codecs (t17) ([#20](https://github.com/mplemay/rowx/pull/20))
- Add temporal encode codecs (t16) ([#19](https://github.com/mplemay/rowx/pull/19))
- Add temporal decode codecs (t15) ([#18](https://github.com/mplemay/rowx/pull/18))
- Add text-like, bytea, uuid and json codecs (t14) ([#17](https://github.com/mplemay/rowx/pull/17))
- Add numeric family codecs (t13) ([#16](https://github.com/mplemay/rowx/pull/16))
- Add parameter extraction and encoding (T12) ([#15](https://github.com/mplemay/rowx/pull/15))
- Add query and query_unchecked (T11) ([#14](https://github.com/mplemay/rowx/pull/14))
- Add row plan and conformance table (T10) ([#13](https://github.com/mplemay/rowx/pull/13))
- Add t-string compilation (T09) ([#12](https://github.com/mplemay/rowx/pull/12))
- Add fetch/execute core with minimal codec (T08) ([#11](https://github.com/mplemay/rowx/pull/11))
- Add async connect, connection and error mapping bindings (T07) ([#10](https://github.com/mplemay/rowx/pull/10))
- Add dsn parsing, connect and error type (T06) ([#9](https://github.com/mplemay/rowx/pull/9))
- Add type-table metadata and core contract (T05) ([#8](https://github.com/mplemay/rowx/pull/8))
- Add ci workflow (T04) ([#7](https://github.com/mplemay/rowx/pull/7))
- Add postgres test infrastructure (T03) ([#6](https://github.com/mplemay/rowx/pull/6))
- Add cargo workspace (T01) and python skeleton (T02) ([#5](https://github.com/mplemay/rowx/pull/5))
- Initial commit

### Bug fixes

- Discard temp tables on pool reset and qualify internal sql
- Validate connect option keys and values, redact extra and dsns
- Harden identifier quoting and template limits
- Harden rowx.testing config, cleanup and loop support ([#179](https://github.com/mplemay/rowx/pull/179))
- Migration lock, transaction probe and cancellation safety ([#176](https://github.com/mplemay/rowx/pull/176))
- Maintenance database selection and scratch database cleanup ([#175](https://github.com/mplemay/rowx/pull/175))
- Sqlx-compatible versions, table names and plan targets ([#174](https://github.com/mplemay/rowx/pull/174))
- Accept any array nesting depth in the checker ([#135](https://github.com/mplemay/rowx/pull/135))
- Address outstanding review comments ([#40](https://github.com/mplemay/rowx/pull/40))
- Start tls postgres in ci for tls tests ([#34](https://github.com/mplemay/rowx/pull/34))

<!-- generated by git-cliff -->
