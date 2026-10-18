from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.db.models import BrowserCollectionRun, Product, Store, StoreSnapshot
from app.db.session import get_session_factory


def _target_ids(snapshot: StoreSnapshot) -> list[str]:
    payload = snapshot.raw_payload if isinstance(snapshot.raw_payload, dict) else {}
    diagnostics = payload.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    values = diagnostics.get("target_ids")
    if not isinstance(values, list):
        raise ValueError(f"快照 {snapshot.id} 缺少 diagnostics.target_ids")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"快照 {snapshot.id} 的商品 ID 含有无效值")
    ids = [value.strip() for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError(f"快照 {snapshot.id} 的商品 ID 为空或重复")
    return ids


def backfill(*, before_run_id: int, expected_stores: int, products_per_store: int, apply: bool) -> dict:
    session_factory = get_session_factory()
    with session_factory() as db:
        try:
            run = db.get(BrowserCollectionRun, before_run_id)
            if run is None:
                raise ValueError(f"找不到采集批次 {before_run_id}")
            stores = list(db.scalars(
                select(Store).where(Store.status == "active").order_by(Store.id)
            ))
            if len(stores) != expected_stores:
                raise ValueError(
                    f"启用店铺数量变化：预期 {expected_stores}，实际 {len(stores)}"
                )

            selected: list[tuple[Store, StoreSnapshot, list[str]]] = []
            globally_seen: set[str] = set()
            for store in stores:
                snapshot = db.scalar(
                    select(StoreSnapshot)
                    .where(
                        StoreSnapshot.store_id == store.id,
                        StoreSnapshot.collected_at < run.created_at,
                    )
                    .order_by(StoreSnapshot.collected_at.desc(), StoreSnapshot.id.desc())
                    .limit(1)
                )
                if snapshot is None:
                    raise ValueError(f"店铺 {store.id} 没有批次前的商品快照")
                ids = _target_ids(snapshot)
                if len(ids) != products_per_store:
                    raise ValueError(
                        f"店铺 {store.id} 快照商品数变化：预期 {products_per_store}，实际 {len(ids)}"
                    )
                duplicate_ids = globally_seen.intersection(ids)
                if duplicate_ids:
                    raise ValueError(
                        f"商品 ID 跨店重复：{', '.join(sorted(duplicate_ids)[:5])}"
                    )
                globally_seen.update(ids)
                selected.append((store, snapshot, ids))

            products_by_store: dict[int, dict[str, Product]] = {}
            for store, _, ids in selected:
                products = list(db.scalars(
                    select(Product).where(
                        Product.store_id == store.id,
                        Product.aliexpress_product_id.in_(ids),
                    )
                ))
                by_external_id = {item.aliexpress_product_id: item for item in products}
                missing = set(ids) - by_external_id.keys()
                if missing:
                    raise ValueError(
                        f"店铺 {store.id} 缺少历史商品记录：{', '.join(sorted(missing)[:5])}"
                    )
                not_inactive = [
                    product.aliexpress_product_id
                    for product in products
                    if product.status != "inactive"
                ]
                if not_inactive:
                    raise ValueError(
                        f"店铺 {store.id} 有商品已非停用状态：{', '.join(sorted(not_inactive)[:5])}"
                    )
                products_by_store[store.id] = by_external_id

            total = sum(len(ids) for _, _, ids in selected)
            if total != expected_stores * products_per_store:
                raise ValueError(f"目标总数校验失败：得到 {total}")

            for store, _, ids in selected:
                for product_id in ids:
                    products_by_store[store.id][product_id].pool_reset_reactivation_eligible = True

            if apply:
                db.commit()
            else:
                db.rollback()

            return {
                "applied": apply,
                "before_run_id": before_run_id,
                "stores": len(selected),
                "products": total,
                "per_store": [
                    {
                        "store_id": store.id,
                        "store_name": store.name,
                        "snapshot_id": snapshot.id,
                        "product_count": len(ids),
                    }
                    for store, snapshot, ids in selected
                ],
            }
        except Exception:
            db.rollback()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill one-time reactivation eligibility from pre-clear pool snapshots."
    )
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--expected-stores", type=int, default=16)
    parser.add_argument("--products-per-store", type=int, default=20)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the verified markers. Without this flag, changes are rolled back.",
    )
    args = parser.parse_args()
    result = backfill(
        before_run_id=args.before_run_id,
        expected_stores=args.expected_stores,
        products_per_store=args.products_per_store,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
