import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.db import queries
from app.db.errors import is_communication_error
from app.db.pool import Db2Pool
from app.errors import ApiError
from app.routes.deps import get_pool

log = logging.getLogger("apiserver.orders")

router = APIRouter(prefix="/api", tags=["orders"])


class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=100)


class OrderIn(BaseModel):
    customer_id: int
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)


@router.post("/orders", status_code=201)
def create_order(body: OrderIn, pool: Db2Pool = Depends(get_pool)):
    wanted: dict[int, int] = {}
    for item in body.items:
        wanted[item.product_id] = wanted.get(item.product_id, 0) + item.quantity
    # Lock rows in a consistent order.
    product_ids = sorted(wanted)

    with pool.connection() as conn:
        queries.begin(conn)
        try:
            if not queries.customer_exists(conn, body.customer_id):
                raise ApiError(
                    404, "customer_not_found", f"customer {body.customer_id} not found", customer_id=body.customer_id
                )

            shortages = []
            for pid in product_ids:
                available = queries.lock_inventory(conn, pid)
                if available is None:
                    raise ApiError(404, "product_not_found", f"product {pid} not found", product_id=pid)
                if available < wanted[pid]:
                    shortages.append({"product_id": pid, "requested": wanted[pid], "available": available})
            if shortages:
                raise ApiError(409, "insufficient_stock", "insufficient stock for one or more items", items=shortages)

            prices = queries.get_product_prices(conn, product_ids)
            total = sum((prices[pid] * wanted[pid] for pid in product_ids), Decimal("0"))
            order = queries.insert_order(conn, body.customer_id, "PLACED", total)
            for pid in product_ids:
                queries.insert_order_item(conn, order["order_id"], pid, wanted[pid], prices[pid])
                queries.decrement_inventory(conn, pid, wanted[pid])
            queries.commit(conn)
        except BaseException:
            try:
                queries.rollback(conn)
            except Exception:
                pass
            raise
        finally:
            try:
                queries.end(conn)
            except Exception as exc:
                if is_communication_error(exc):
                    raise
                log.warning("failed to restore autocommit", exc_info=True)

    log.info("order placed", extra={"order_id": order["order_id"], "customer_id": body.customer_id, "total": float(total)})
    return {
        "order_id": order["order_id"],
        "customer_id": body.customer_id,
        "order_date": order["order_date"],
        "status": "PLACED",
        "total": float(total),
        "items": [
            {"product_id": pid, "quantity": wanted[pid], "unit_price": float(prices[pid])} for pid in product_ids
        ],
    }


@router.get("/orders")
def list_orders(
    customer_id: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    pool: Db2Pool = Depends(get_pool),
):
    with pool.connection() as conn:
        if customer_id is not None:
            orders = queries.list_orders_for_customer(conn, customer_id, limit)
        else:
            orders = queries.list_recent_orders(conn, limit)
        items = queries.get_order_items(conn, [o["order_id"] for o in orders]) if orders else []

    by_order: dict[int, list] = {o["order_id"]: [] for o in orders}
    for item in items:
        order_id = item.pop("order_id")
        by_order[order_id].append(item)
    for o in orders:
        o["items"] = by_order[o["order_id"]]
    return {"items": orders}
