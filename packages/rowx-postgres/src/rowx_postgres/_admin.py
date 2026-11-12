from typing import Final
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse

from rowx_postgres._options import ConnectOptions

URL_SCHEMES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")


# `dsn` (a URL or a key=value string) pointed at another database, as a string the driver reads back as `name`
def with_database(dsn: str, name: str) -> str:
    dsn = dsn.strip()
    if dsn.startswith(URL_SCHEMES):
        parts = urlparse(dsn)
        # the driver percent-decodes query keys and applies them in order after the path, the last one winning, so
        # every spelling of `dbname` (`%64bname` too) is dropped and the new name goes last; `quote` because the
        # driver doesn't read `+` as a space
        kept = [pair for pair in parts.query.split("&") if pair and unquote(pair.partition("=")[0]) != "dbname"]
        query = "&".join([*kept, urlencode({"dbname": name}, quote_via=quote)])
        url = urlunparse(parts._replace(path=f"/{quote(name, safe='')}", query=query))
    else:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        url = f"{dsn} dbname='{escaped}'"
    # a guard against any other way the URL could still select a different database
    if ConnectOptions.from_dsn(url).database != name:
        msg = "the database URL could not be pointed at another database"
        raise ValueError(msg)
    return url
