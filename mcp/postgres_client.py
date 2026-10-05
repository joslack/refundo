import logging
from typing import Any, List, Optional
import asyncpg

# Configure minimal logging to track connections/errors
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AsyncPostgresClient")

class AsyncPostgresClient:
    def __init__(self, dsn: Optional[str] = None, **config: Any):
        """
        Initializes the client config.
        :param dsn: A Postgres connection string (e.g., 'postgres://user:pass@host:5432/db')
        :param config: Keyword arguments matching asyncpg.create_pool params (host, user, etc.)
        """
        self.dsn = dsn
        self.config = config
        self._pool: Optional[asyncpg.Pool] = None

    async def setup(self) -> None:
        """Creates the underlying connection pool."""
        if self._pool is None:
            if self.dsn:
                self._pool = await asyncpg.create_pool(dsn=self.dsn, **self.config)
            else:
                self._pool = await asyncpg.create_pool(**self.config)
            logger.info("Database connection pool established.")

    async def close(self) -> None:
        """Gracefully closes all connections in the pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Database connection pool closed.")

    async def execute(self, query: str, *args: Any) -> str:
        """
        Executes a command (e.g., CREATE TABLE, INSERT, UPDATE, DELETE).
        Returns the command status string (e.g., 'INSERT 0 1').
        """
        if self._pool is None:
            raise RuntimeError("Pool is not initialized. Run client.setup() first.")
        
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> List[asyncpg.Record]:
        """Executes a query and returns all resulting rows."""
        if self._pool is None:
            raise RuntimeError("Pool is not initialized. Run client.setup() first.")
            
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Executes a query and returns the first row, or None."""
        if self._pool is None:
            raise RuntimeError("Pool is not initialized. Run client.setup() first.")
            
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any, column: int = 0) -> Any:
        """Executes a query and returns the value of the first column of the first row."""
        if self._pool is None:
            raise RuntimeError("Pool is not initialized. Run client.setup() first.")
            
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args, column=column)
