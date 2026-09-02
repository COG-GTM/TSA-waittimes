from app import db


def test_pool_connection_kwargs_bound_hung_sockets() -> None:
    assert db.POOL_KWARGS == {
        "options": "-c timezone=UTC",
        "tcp_user_timeout": 15000,
        "keepalives": 1,
        "keepalives_idle": 5,
        "keepalives_interval": 2,
        "keepalives_count": 3,
    }
