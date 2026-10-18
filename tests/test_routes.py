from decimal import Decimal

from app.db.pool import POOL_CONNECTIONS_CLOSED
from tests.test_errors import LOCK_TIMEOUT_MSG

PRODUCT_ROW = {
    "PRODUCT_ID": 7,
    "NAME": "Wireless Headphones",
    "DESCRIPTION": "Nice",
    "CATEGORY": "Electronics",
    "PRICE": Decimal("99.99"),
    "IMAGE_URL": "https://example.com/7.jpg",
    "IN_STOCK": 12,
}


def test_healthz_does_not_touch_db(client, fake_db):
    fake_db.STATE.executed.clear()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert fake_db.STATE.executed == []


def test_request_id_is_propagated(client):
    resp = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"


def test_request_id_is_generated(client):
    resp = client.get("/healthz")
    assert len(resp.headers["x-request-id"]) == 32


def test_readyz_ok(client, fake_db):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert any("SYSIBM.SYSDUMMY1" in sql for sql, _ in fake_db.STATE.executed)


def test_readyz_db_down(client, fake_db):
    fake_db.on("SYSIBM.SYSDUMMY1", Exception("SQL30081N  A communication error. SQLSTATE=08001 SQLCODE=-30081"))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["sqlcode"] == -30081
    assert body["request_id"] == resp.headers["x-request-id"]


def test_list_products_joins_inventory(client, fake_db):
    fake_db.on("FROM COMMERCE.PRODUCTS p JOIN COMMERCE.INVENTORY i", [PRODUCT_ROW])
    resp = client.get("/api/products?limit=5&offset=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [
        {
            "product_id": 7,
            "name": "Wireless Headphones",
            "description": "Nice",
            "category": "Electronics",
            "price": 99.99,
            "image_url": "https://example.com/7.jpg",
            "in_stock": 12,
        }
    ]
    sql, params = fake_db.STATE.executed[-1]
    assert params == (10, 5)


def test_list_products_by_category(client, fake_db):
    fake_db.on("WHERE p.category = ?", [PRODUCT_ROW])
    resp = client.get("/api/products?category=Electronics")
    assert resp.status_code == 200
    assert fake_db.STATE.executed[-1][1] == ("Electronics", 0, 20)


def test_get_product_not_found(client):
    resp = client.get("/api/products/999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "product_not_found"
    assert body["request_id"]


def test_get_product_not_found_does_not_leak_connection(make_client):
    """Test that 404 on missing product doesn't leak a connection from the pool.
    
    This is a regression test for a connection leak where pool.acquire() was called
    without proper try-finally or context manager, causing connections to accumulate
    when products weren't found.
    """
    client = make_client(pool_max_size=3)
    pool = client.app.state.pool
    
    # Verify initial pool state
    assert pool._in_use == 0
    
    # Make multiple requests for non-existent products
    for i in range(5):
        resp = client.get(f"/api/products/{999 + i}")
        assert resp.status_code == 404
        # After each request completes, all acquired connections should be released
        assert pool._in_use == 0, f"Connection leaked after request {i}"
    
    # Pool should be able to handle more requests without timeouts
    assert pool._in_use == 0


def test_validation_error_includes_request_id(client):
    resp = client.get("/api/products?limit=0")
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    assert resp.json()["request_id"] == resp.headers["x-request-id"]


def test_create_order_success(client, fake_db):
    fake_db.on("FROM COMMERCE.CUSTOMERS WHERE customer_id = ?", [{"FOUND": 1}])
    fake_db.on("FROM COMMERCE.INVENTORY WHERE product_id = ? FOR UPDATE", [{"QUANTITY": 10}])
    fake_db.on(
        "SELECT product_id, price FROM COMMERCE.PRODUCTS",
        [{"PRODUCT_ID": 1, "PRICE": Decimal("5.00")}, {"PRODUCT_ID": 2, "PRICE": Decimal("2.50")}],
    )
    fake_db.on("FINAL TABLE", [{"ORDER_ID": 555, "ORDER_DATE": "2026-09-30T12:00:00"}])
    fake_db.on("INSERT INTO COMMERCE.ORDER_ITEMS", 1)
    fake_db.on("UPDATE COMMERCE.INVENTORY", 1)

    resp = client.post(
        "/api/orders",
        json={"customer_id": 3, "items": [{"product_id": 2, "quantity": 2}, {"product_id": 1, "quantity": 1}]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["order_id"] == 555
    assert body["total"] == 10.0
    assert "commit" in fake_db.STATE.calls
    assert "rollback" not in fake_db.STATE.calls
    insert_sql, insert_params = next(e for e in fake_db.STATE.executed if "FINAL TABLE" in e[0])
    assert insert_params == (3, "PLACED", "10.00")


def test_create_order_insufficient_stock_returns_409(client, fake_db):
    fake_db.on("FROM COMMERCE.CUSTOMERS WHERE customer_id = ?", [{"FOUND": 1}])
    fake_db.on("FROM COMMERCE.INVENTORY WHERE product_id = ? FOR UPDATE", [{"QUANTITY": 1}])

    resp = client.post("/api/orders", json={"customer_id": 3, "items": [{"product_id": 1, "quantity": 5}]})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "insufficient_stock"
    assert body["items"] == [{"product_id": 1, "requested": 5, "available": 1}]
    assert body["request_id"]
    assert "rollback" in fake_db.STATE.calls
    assert "commit" not in fake_db.STATE.calls
    assert not any("INSERT INTO COMMERCE.ORDERS" in sql for sql, _ in fake_db.STATE.executed)


def test_lock_timeout_maps_to_503(client, fake_db):
    fake_db.on("FROM COMMERCE.CUSTOMERS WHERE customer_id = ?", [{"FOUND": 1}])
    fake_db.on("FOR UPDATE WITH RS", Exception(LOCK_TIMEOUT_MSG))

    resp = client.post("/api/orders", json={"customer_id": 3, "items": [{"product_id": 1, "quantity": 1}]})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "db_lock_timeout"
    assert body["sqlcode"] == -911
    assert body["reason"] == 68
    assert body["request_id"] == resp.headers["x-request-id"]


def test_other_db_error_maps_to_500(client, fake_db):
    fake_db.on("FROM COMMERCE.ORDERS o", Exception('SQL0204N  "COMMERCE.ORDERS" is an undefined name.  SQLSTATE=42704 SQLCODE=-204'))
    resp = client.get("/api/orders?customer_id=1")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "db_error"
    assert body["sqlcode"] == -204
    assert body["request_id"]


def test_pool_timeout_maps_to_503(make_client):
    client = make_client(pool_max_size=1, pool_acquire_timeout=0.05)
    held = client.app.state.pool.acquire()
    try:
        resp = client.get("/api/customers")
    finally:
        client.app.state.pool.release(held)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "db_pool_timeout"
    assert body["request_id"]


def test_communication_error_closes_connection(client, fake_db):
    before = POOL_CONNECTIONS_CLOSED._value.get()
    fake_db.on("FROM COMMERCE.CUSTOMERS c", Exception("SQL30081N  A communication error. SQLSTATE=08001 SQLCODE=-30081"))
    resp = client.get("/api/customers")
    assert resp.status_code == 500
    assert POOL_CONNECTIONS_CLOSED._value.get() == before + 1


def test_list_orders_groups_items(client, fake_db):
    fake_db.on(
        "ORDER BY o.order_date DESC",
        [
            {"ORDER_ID": 2, "CUSTOMER_ID": 1, "ORDER_DATE": "2026-09-02", "STATUS": "PLACED", "TOTAL": Decimal("3.00")},
            {"ORDER_ID": 1, "CUSTOMER_ID": 1, "ORDER_DATE": "2026-09-01", "STATUS": "DELIVERED", "TOTAL": Decimal("4.00")},
        ],
    )
    fake_db.on(
        "FROM COMMERCE.ORDER_ITEMS oi",
        [
            {"ORDER_ID": 1, "PRODUCT_ID": 9, "NAME": "Honey", "QUANTITY": 2, "UNIT_PRICE": "2.00"},
            {"ORDER_ID": 2, "PRODUCT_ID": 8, "NAME": "Tea", "QUANTITY": 1, "UNIT_PRICE": "3.00"},
        ],
    )
    resp = client.get("/api/orders")
    assert resp.status_code == 200
    orders = resp.json()["items"]
    assert [o["order_id"] for o in orders] == [2, 1]
    assert orders[1]["items"] == [{"product_id": 9, "name": "Honey", "quantity": 2, "unit_price": 2.0}]


def test_metrics_use_route_template(client, fake_db):
    client.get("/api/products/12345")
    text = client.get("/metrics").text
    assert 'route="/api/products/{id}"' in text
    assert "/api/products/12345" not in text
    assert "apiserver_db_pool_max_size" in text
    assert 'apiserver_db_query_seconds_count{query="get_product"}' in text
