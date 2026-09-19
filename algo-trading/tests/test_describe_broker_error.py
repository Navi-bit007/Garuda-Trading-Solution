from app.execution.trailing_stop_agent import _describe_broker_error


class RemoteDisconnected(Exception):
    pass


def test_translates_a_raw_connection_exception_repr():
    error = ("Connection aborted.", RemoteDisconnected("Remote end closed connection without response"))

    message = _describe_broker_error(error)

    assert "Lost connection to Zerodha" in message
    assert "RemoteDisconnected" not in message


def test_translates_by_exception_type_name():
    message = _describe_broker_error(RemoteDisconnected("anything"))

    assert "Lost connection to Zerodha" in message


def test_translates_an_expired_token_message():
    message = _describe_broker_error("access_token is invalid or has expired")

    assert "log in to Kite again" in message


def test_leaves_a_clean_kite_message_unchanged():
    message = _describe_broker_error("Couldn't find that order_id")

    assert message == "Couldn't find that order_id"
