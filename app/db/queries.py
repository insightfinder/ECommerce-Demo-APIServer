"""All SQL used by the API. One function per query; every execution is timed."""

import datetime as dt
import logging
import time
from decimal import Decimal

import ibm_db
from prometheus_client import Counter, Histogram

from app.db.errors import DbError, parse_db2_error
from app.logging_setup import add_db_time

log = logging.getLogger("apiserver.db.queries")

QUERY_SECONDS = Histogram(
    "apiserver_db_query_seconds",
    "Time spent preparing, executing and fetching a query",
    ["query"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
DB_ERRORS = Counter("apiserver_db_errors_total", "Db2 errors by query and SQLCODE", ["query", "sqlcode"])

_MONEY_COLUMNS = {"price", "total", "unit_price"}


def _normalize(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        key = key.lower()
        if isinstance(value, (dt.datetime, dt.date)):
            value = value.isoformat()
        elif key in _MONEY_COLUMNS and isinstance(value, str):
            value = Decimal(value)
        out[key] = value
    return out


def _error(query: str, conn, stmt, exc: Exception) -> DbError:
    msg = None
    try:
        msg = ibm_db.stmt_errormsg(stmt) if stmt else ibm_db.conn_errormsg(conn)
    except Exception:
        pass
    info = parse_db2_error(msg)
    if info.sqlcode is None:
        info = parse_db2_error(str(exc))
    DB_ERRORS.labels(query=query, sqlcode=str(info.sqlcode) if info.sqlcode is not None else "unknown").inc()
    log.error(
        "db query failed",
        extra={"query": query, "sqlcode": info.sqlcode, "sqlstate": info.sqlstate, "reason": info.reason},
        exc_info=exc,
    )
    return DbError(query, info)


def _run(query: str, conn, sql: str, params=(), fetch: bool = True):
    """Prepare and execute `sql`; return normalized rows, or the affected row count if not fetching."""
    stmt = None
    start = time.perf_counter()
    try:
        stmt = ibm_db.prepare(conn, sql)
        ibm_db.execute(stmt, tuple(params))
        if not fetch:
            return ibm_db.num_rows(stmt)
        rows = []
        row = ibm_db.fetch_assoc(stmt)
        while row:
            rows.append(_normalize(row))
            row = ibm_db.fetch_assoc(stmt)
        return rows
    except Exception as exc:
        raise _error(query, conn, stmt, exc) from exc
    finally:
        elapsed = time.perf_counter() - start
        QUERY_SECONDS.labels(query=query).observe(elapsed)
        add_db_time(elapsed)
        if stmt:
            try:
                ibm_db.free_stmt(stmt)
            except Exception:
                pass


def _timed_call(query: str, conn, fn, *args):
    start = time.perf_counter()
    try:
        return fn(conn, *args)
    except Exception as exc:
        raise _error(query, conn, None, exc) from exc
    finally:
        elapsed = time.perf_counter() - start
        QUERY_SECONDS.labels(query=query).observe(elapsed)
        add_db_time(elapsed)


def _markers(n: int) -> str:
    return ", ".join("?" * n)


# --- transactions -----------------------------------------------------------

def begin(conn) -> None:
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)


def end(conn) -> None:
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)


def commit(conn) -> None:
    _timed_call("commit", conn, ibm_db.commit)


def rollback(conn) -> None:
    _timed_call("rollback", conn, ibm_db.rollback)


# --- health -----------------------------------------------------------------

def ping(conn) -> None:
    _run("ping", conn, "SELECT 1 FROM SYSIBM.SYSDUMMY1")


# --- catalog ----------------------------------------------------------------

_PRODUCT_COLUMNS = """
    p.product_id, p.name, p.description, p.category, p.price, p.image_url,
    i.quantity AS in_stock
"""


def list_categories(conn) -> list[dict]:
    return _run(
        "list_categories",
        conn,
        """
        SELECT p.category, COUNT(*) AS product_count, SUM(CASE WHEN i.quantity > 0 THEN 1 ELSE 0 END) AS in_stock_count
        FROM COMMERCE.PRODUCTS p
        JOIN COMMERCE.INVENTORY i ON i.product_id = p.product_id
        GROUP BY p.category
        ORDER BY p.category
        """,
    )


def list_products(conn, limit: int, offset: int) -> list[dict]:
    return _run(
        "list_products",
        conn,
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM COMMERCE.PRODUCTS p
        JOIN COMMERCE.INVENTORY i ON i.product_id = p.product_id
        ORDER BY p.product_id
        OFFSET ? ROWS FETCH FIRST ? ROWS ONLY
        """,
        (offset, limit),
    )


def list_products_by_category(conn, category: str, limit: int, offset: int) -> list[dict]:
    return _run(
        "list_products_by_category",
        conn,
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM COMMERCE.PRODUCTS p
        JOIN COMMERCE.INVENTORY i ON i.product_id = p.product_id
        WHERE p.category = ?
        ORDER BY p.product_id
        OFFSET ? ROWS FETCH FIRST ? ROWS ONLY
        """,
        (category, offset, limit),
    )


def get_product(conn, product_id: int) -> dict | None:
    rows = _run(
        "get_product",
        conn,
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM COMMERCE.PRODUCTS p
        JOIN COMMERCE.INVENTORY i ON i.product_id = p.product_id
        WHERE p.product_id = ?
        """,
        (product_id,),
    )
    return rows[0] if rows else None


# --- customers --------------------------------------------------------------

def list_customers(conn, limit: int) -> list[dict]:
    return _run(
        "list_customers",
        conn,
        """
        SELECT c.customer_id, c.name, c.email, c.created_at,
               COALESCE(order_counts.order_count, 0) AS order_count
        FROM COMMERCE.CUSTOMERS c
        LEFT JOIN (
            SELECT customer_id, COUNT(*) AS order_count
            FROM COMMERCE.ORDERS
            GROUP BY customer_id
        ) order_counts ON order_counts.customer_id = c.customer_id
        ORDER BY c.customer_id
        FETCH FIRST ? ROWS ONLY
        """,
        (limit,),
    )


def customer_exists(conn, customer_id: int) -> bool:
    rows = _run(
        "customer_exists",
        conn,
        "SELECT 1 AS found FROM COMMERCE.CUSTOMERS WHERE customer_id = ?",
        (customer_id,),
    )
    return bool(rows)


# --- orders -----------------------------------------------------------------

def lock_inventory(conn, product_id: int) -> int | None:
    """Lock one INVENTORY row for the rest of the transaction; return its quantity."""
    rows = _run(
        "lock_inventory",
        conn,
        "SELECT quantity FROM COMMERCE.INVENTORY WHERE product_id = ? FOR UPDATE WITH RS",
        (product_id,),
    )
    return rows[0]["quantity"] if rows else None


def get_product_prices(conn, product_ids: list[int]) -> dict[int, Decimal]:
    rows = _run(
        "get_product_prices",
        conn,
        f"SELECT product_id, price FROM COMMERCE.PRODUCTS WHERE product_id IN ({_markers(len(product_ids))})",
        product_ids,
    )
    return {r["product_id"]: Decimal(r["price"]) for r in rows}


def insert_order(conn, customer_id: int, status: str, total: Decimal) -> dict:
    rows = _run(
        "insert_order",
        conn,
        """
        SELECT order_id, order_date FROM FINAL TABLE (
            INSERT INTO COMMERCE.ORDERS (customer_id, order_date, status, total)
            VALUES (?, CURRENT TIMESTAMP, ?, ?)
        )
        """,
        (customer_id, status, str(total)),
    )
    return rows[0]


def insert_order_item(conn, order_id: int, product_id: int, quantity: int, unit_price: Decimal) -> None:
    _run(
        "insert_order_item",
        conn,
        "INSERT INTO COMMERCE.ORDER_ITEMS (order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
        (order_id, product_id, quantity, str(unit_price)),
        fetch=False,
    )


def decrement_inventory(conn, product_id: int, quantity: int) -> int:
    return _run(
        "decrement_inventory",
        conn,
        """
        UPDATE COMMERCE.INVENTORY
        SET quantity = quantity - ?, updated_at = CURRENT TIMESTAMP
        WHERE product_id = ?
        """,
        (quantity, product_id),
        fetch=False,
    )


_ORDER_COLUMNS = "o.order_id, o.customer_id, o.order_date, o.status, o.total"


def list_orders_for_customer(conn, customer_id: int, limit: int) -> list[dict]:
    return _run(
        "list_orders_for_customer",
        conn,
        f"""
        SELECT {_ORDER_COLUMNS}
        FROM COMMERCE.ORDERS o
        WHERE o.customer_id = ?
        ORDER BY o.order_date DESC
        FETCH FIRST ? ROWS ONLY
        """,
        (customer_id, limit),
    )


def list_recent_orders(conn, limit: int) -> list[dict]:
    return _run(
        "list_recent_orders",
        conn,
        f"""
        SELECT {_ORDER_COLUMNS}
        FROM COMMERCE.ORDERS o
        ORDER BY o.order_date DESC
        FETCH FIRST ? ROWS ONLY
        """,
        (limit,),
    )


def get_order_items(conn, order_ids: list[int]) -> list[dict]:
    return _run(
        "get_order_items",
        conn,
        f"""
        SELECT oi.order_id, oi.product_id, p.name, oi.quantity, oi.unit_price
        FROM COMMERCE.ORDER_ITEMS oi
        JOIN COMMERCE.PRODUCTS p ON p.product_id = oi.product_id
        WHERE oi.order_id IN ({_markers(len(order_ids))})
        ORDER BY oi.order_id, oi.product_id
        """,
        order_ids,
    )
