# Maintenance constraints / 维护约束

This public file contains project rules only. Never add credentials, private paths,
market data, personal settings, results, logs, or deployment details.

第五批固定保留39项，不能恢复其他81项，也不能添加绕过开关：
015、022、023、025、026、027、028、029、031、034、035、036、037、038、039、040、041、051、055、059、070、071、072、073、077、079、081、084、085、086、087、088、090、096、097、098、099、101、102。

The allowlist in `fifth_policy.py` applies to UI, selection, fingerprints, caches,
and execution. Preserve historical CSV formats, configuration signatures, and
fingerprint semantics. Do not mix source versions during an active backtest.

Keep changes small. Run `python run_self_tests.py` after functional changes.
Use synthetic test data and temporary directories. Never read or overwrite user
preferences during tests. State which operating systems were actually tested.
