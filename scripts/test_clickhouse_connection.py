import os
import sys

import clickhouse_connect
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ["CLICKHOUSE_HTTP_PORT"]),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database=os.environ["CLICKHOUSE_DATABASE"],
    )

    result = client.query(
        """
        SELECT
            version(),
            currentDatabase(),
            currentUser()
        """
    )

    print(result.result_rows[0])
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
