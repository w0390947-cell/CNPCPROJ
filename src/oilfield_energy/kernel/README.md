# Shared numerical values

Owners: 项目技术负责人. Reviewer: 潮流计算维护负责人.

`intervals.Interval` validates finite ordered bounds. `enclosures` converts
exact rationals outwards and encloses square roots with integer arithmetic.
These primitives are shared by device propagation and AC proof rules. No
business module, workflow, I/O or numerical SDK may be imported here.
