from rowx import query


def run(user_id: int | None, name: str | None, title: str | None, note: str | None) -> None:
    query[tuple[str, str | None]](
        t"SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = {user_id}"
    )
    query[tuple[str, str | None]](
        t"SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id AND p.title = {title}"
    )
    query[tuple[str | None, str]](
        t"SELECT p.title, u.name FROM posts p RIGHT JOIN users u ON p.user_id = u.id WHERE u.name = {name}"
    )
    query[tuple[str | None, str | None]](
        t"SELECT u.name, p.title FROM users u FULL JOIN posts p ON p.title = u.email AND u.name = {name}"
    )
    query[tuple[str, str | None]](
        t"SELECT u.name, o.note FROM users u LEFT JOIN orders o ON o.note = u.email WHERE o.note IS NOT DISTINCT FROM {note}"
    )
    query[tuple[str, str | None]](
        t"SELECT u.name, o.note FROM users u LEFT JOIN orders o ON o.note = {note} ORDER BY o.note"
    )
    query[tuple[str | None, int | None]](
        t"SELECT x.title, x.id FROM (SELECT p.title, p.id, row_number() OVER () FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = {user_id}) x"
    )
    query[tuple[str, str]](
        t"SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE p.title = {title}"
    )
