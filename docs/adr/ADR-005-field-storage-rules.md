# ADR-005 核心字段独立存储，metadata_json 仅存扩展属性
状态：已接受（2026-11-02）

## 决定
tags → memory_tags；sources → memory_sources；parent_ids / reference_ids → memory_edges（PARENT：父→子；REFERENCE：引用者→被引用）。metadata_json 只存非核心扩展属性；DTO 拒绝以核心字段名作为 metadata 键。

## 理由
核心关系与高频查询字段放入 JSON 无法建索引、无法加约束，也可能绕过环检测与 ACL。

## 影响
- 读取记忆时由关联表组装 tags、sources、parent_ids、reference_ids。
- 按 tag 查询走 ix_memory_tags_tag 索引。

## 替代方案
- tags 存 JSON 数组：查询低效、无约束。
- parent_ids 单独表：与 DAG 重复，破坏“边统一经 GraphService”的规则。
