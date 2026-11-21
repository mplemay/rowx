from rowx_embedded._backend import load


def test_an_embedded_memory_url_is_in_memory_and_a_directory_is_not() -> None:
    backend = load()
    assert backend.in_memory("embedded://") is True
    assert backend.in_memory("embedded:///var/db") is False
    assert backend.in_memory("postgresql://h/db") is False


def test_the_embedded_backend_has_no_shared_server_warning() -> None:
    assert load().compat_warning is None
