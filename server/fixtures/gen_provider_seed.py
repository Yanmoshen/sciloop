# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""从 Cherry Studio 的 provider-registry 生成 7 家主流供应商的预置 SQL。

数据源（只读，不修改）::

    <registry>/providers.json          61 家供应商（defaultChatEndpoint / endpointConfigs）
    <registry>/models.json             914 个目录模型（含 pricing）
    <registry>/provider-models.json    1404 条供应商覆盖（含 pricing）

预置的 7 家：``deepseek`` ``openai`` ``anthropic`` ``zhipu`` ``moonshot`` ``silicon`` ``dashscope``

口径（逐条说明，便于复核）
--------------------------
``name``
    取 Cherry 的 ``name``。
``type``
    ``defaultChatEndpoint`` 映射：``openai-chat-completions`` / ``openai-responses`` → ``openai``；
    ``anthropic-messages`` → ``anthropic``；其余端点类型原样保留端点 key。
``base_url``
    取 ``endpointConfigs[defaultChatEndpoint].baseUrl``，去掉尾斜杠。
``models[]`` 的 ``model_id`` / ``label`` / ``pricing``
    以 ``provider-models.json`` 中该 provider 的覆盖条为主：``model_id = apiModelId``，
    ``label = name``（覆盖条缺 ``name`` 时回落到目录名，再回落到 id），``pricing`` **原样搬**。
    **覆盖条缺 pricing 时用 `models.json` 目录里同一 ``modelId`` 的 pricing 回填**
    —— 这是 Cherry 自己的基础层数据（覆盖条之上再叠加），不是本项目估算；
    两处都没有 pricing 时保留 ``pricing: {}``（不猜价格，运行期 cost_usd 记 null）。
    去重按 ``model_id``（``apiModelId``）。

    例外：``openai`` / ``anthropic`` 在 ``provider-models.json`` 里**没有任何覆盖条**，
    此时回落到 ``models.json`` 中 ``ownedBy`` 等于该 provider id 的目录模型
    （``model_id = id``，``label = name``，``pricing`` 原样搬）。
``api_key_enc``
    留空（用户自行在设置页填写）；``is_default`` 全为 false。

可重复执行
----------
生成的 SQL **只 INSERT、不 DELETE**，每条都以
``WHERE NOT EXISTS (SELECT 1 FROM model_configs WHERE name = '<name>')`` 兜底，
因此重复执行不会产生重复行（幂等）。

用法::

    python app/fixtures/gen_provider_seed.py                     # 生成 SQL 到同目录
    python app/fixtures/gen_provider_seed.py --registry <dir>     # 指定 registry 目录
    python app/fixtures/gen_provider_seed.py --out <path.sql>     # 指定输出文件
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = Path("D:/cherry studio/resources/provider-registry")

TARGET_PROVIDER_IDS: tuple[str, ...] = (
    "deepseek",
    "openai",
    "anthropic",
    "zhipu",
    "moonshot",
    "silicon",
    "dashscope",
)

#: Cherry ``defaultChatEndpoint`` → SciLoop ``model_configs.type``
ENDPOINT_TYPE_MAP: dict[str, str] = {
    "openai-chat-completions": "openai",
    "openai-responses": "openai",
    "anthropic-messages": "anthropic",
    "google-generate-content": "google",
}

_FIXTURE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = _FIXTURE_DIR / "seed_providers_7.sql"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _models_url_safe(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def build_provider_rows(registry: Path) -> list[dict[str, Any]]:
    """组装 7 家供应商的预置行（纯数据，便于测试与复核）。"""
    providers = _load(registry / "providers.json")["providers"]
    catalog = _load(registry / "models.json")["models"]
    overrides = _load(registry / "provider-models.json")["overrides"]

    catalog_by_id = {str(m.get("id")): m for m in catalog}
    overrides_by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in overrides:
        overrides_by_provider.setdefault(str(row.get("providerId")), []).append(row)

    rows: list[dict[str, Any]] = []
    for provider in providers:
        provider_id = str(provider.get("id") or "")
        if provider_id not in TARGET_PROVIDER_IDS:
            continue
        endpoint_key = provider.get("defaultChatEndpoint")
        endpoint_configs = provider.get("endpointConfigs") or {}
        endpoint = endpoint_configs.get(endpoint_key) or {}
        base_url = _models_url_safe(endpoint.get("baseUrl") or "")
        if not base_url:
            raise SystemExit(f"provider {provider_id} 缺少 baseUrl，无法预置")

        models = _merge_models(
            provider_id=provider_id,
            override_rows=overrides_by_provider.get(provider_id, []),
            catalog_by_id=catalog_by_id,
        )
        rows.append(
            {
                "id": provider_id,
                "name": str(provider.get("name") or provider_id),
                "type": ENDPOINT_TYPE_MAP.get(str(endpoint_key), endpoint_key),
                "base_url": base_url,
                "endpoint_key": endpoint_key,
                "models": models,
            }
        )

    order = {pid: index for index, pid in enumerate(TARGET_PROVIDER_IDS)}
    rows.sort(key=lambda row: order[row["id"]])
    missing = [pid for pid in TARGET_PROVIDER_IDS if pid not in {row["id"] for row in rows}]
    if missing:
        raise SystemExit(f"registry 中找不到这些供应商：{missing}")
    return rows


def _merge_models(
    *,
    provider_id: str,
    override_rows: list[dict[str, Any]],
    catalog_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in override_rows:
        model_id = str(row.get("apiModelId") or row.get("modelId") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        catalog_entry = catalog_by_id.get(str(row.get("modelId"))) or {}
        pricing = row.get("pricing")
        if not isinstance(pricing, dict) or not pricing:
            pricing = catalog_entry.get("pricing")
        entries.append(
            {
                "model_id": model_id,
                "label": str(row.get("name") or catalog_entry.get("name") or model_id),
                "pricing": pricing if isinstance(pricing, dict) else {},
            }
        )

    if not entries:
        # openai / anthropic 无覆盖条：回落到目录里 ownedBy 等于该 provider 的模型
        for item in catalog_by_id.values():
            if str(item.get("ownedBy")) != provider_id:
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            pricing = item.get("pricing")
            entries.append(
                {
                    "model_id": model_id,
                    "label": str(item.get("name") or model_id),
                    "pricing": pricing if isinstance(pricing, dict) else {},
                }
            )
    return entries


def _sql_literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def render_sql(rows: list[dict[str, Any]], *, registry: Path) -> str:
    lines: list[str] = [
        "-- Copyright 2026 SciLoop contributors",
        "-- 由 app/fixtures/gen_provider_seed.py 生成（**请勿手改**，改脚本后重新生成）",
        f"-- 数据源：{registry}",
        "-- 只 INSERT 不 DELETE；每条带 NOT EXISTS 兜底，可重复执行（幂等）。",
        "-- api_key_enc 一律留空：Key 由用户在设置页自行填写，预置数据不含任何凭据。",
        "",
        "BEGIN;",
        "",
    ]
    for row in rows:
        payload = json.dumps(row["models"], ensure_ascii=False, separators=(",", ":"))
        lines.append(
            "INSERT INTO model_configs (name, base_url, api_key_enc, models, is_default, type)\n"
            f"SELECT {_sql_literal(row['name'])}, {_sql_literal(row['base_url'])}, '', "
            f"CAST({_sql_literal(payload)} AS JSONB), false, {_sql_literal(row['type'])}\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM model_configs WHERE name = {_sql_literal(row['name'])});"
        )
        lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def summarize(rows: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for row in rows:
        with_pricing = sum(1 for m in row["models"] if m["pricing"])
        currencies = Counter(
            str((m["pricing"].get("input") or {}).get("currency") or "?")
            for m in row["models"]
            if m["pricing"]
        )
        out.append(
            f"{row['id']:<10} type={str(row['type']):<9} base_url={row['base_url']:<45} "
            f"models={len(row['models']):>3} with_pricing={with_pricing:>3} "
            f"currencies={dict(currencies)}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 7 家供应商预置 SQL")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not (args.registry / "providers.json").is_file():
        raise SystemExit(f"registry 目录不可用：{args.registry}")

    rows = build_provider_rows(args.registry)
    args.out.write_text(render_sql(rows, registry=args.registry), encoding="utf-8")
    print(summarize(rows))
    print(f"\n已写出 {args.out}（{len(rows)} 家）")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
