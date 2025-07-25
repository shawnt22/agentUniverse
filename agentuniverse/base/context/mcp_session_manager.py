# !/usr/bin/env python3
# -*- coding:utf-8 -*-
import asyncio
import os
from contextlib import AsyncExitStack, ExitStack
# @Time    : 2024/3/11 16:02
# @Author  : fanen.lhy
# @Email   : fanen.lhy@antgroup.com
# @FileName: mcp_session_manager.py
from contextvars import ContextVar
from types import TracebackType
from typing import Callable, Any
from typing import Literal, cast
from datetime import timedelta

from anyio.from_thread import start_blocking_portal
from mcp import StdioServerParameters, stdio_client, ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from agentuniverse.base.annotation.singleton import singleton

EncodingErrorHandler = Literal["strict", "ignore", "replace"]

DEFAULT_ENCODING = "utf-8"
DEFAULT_ENCODING_ERROR_HANDLER: EncodingErrorHandler = "strict"

DEFAULT_HTTP_TIMEOUT = 5
DEFAULT_SSE_READ_TIMEOUT = 60 * 5

DEFAULT_STREAMABLE_HTTP_TIMEOUT = timedelta(seconds=30)
DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT = timedelta(seconds=60 * 5)

class SyncAsyncExitStack:
    def __init__(self) -> None:
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        self._stack: ExitStack = ExitStack()

    def run_async(self, func, *args, **kwargs):
        """use portal to run async code"""
        return self._portal.call(func, *args, **kwargs)

    def enter_async_context(self, async_cm):
        sync_cm = self._portal.wrap_async_context_manager(async_cm)
        return self._stack.enter_context(sync_cm)

    def callback(self, func: Callable, *args, **kwargs):
        return self._portal.call(self._stack.callback, func, *args, **kwargs)

    def close(self):
        try:
            self._stack.close()
        finally:
            self._portal_cm.__exit__(None, None, None)


def pick_exit_stack():
    """
    return SyncAsyncExitStack in sync mode otherwise AsyncExitStack
    """
    try:
        asyncio.get_running_loop()
        return AsyncExitStack()
    except RuntimeError:
        return SyncAsyncExitStack()


class MCPTempClient:
    def __init__(self, connection_args: dict):
        self.exit_stack = AsyncExitStack()
        self.connection_args = connection_args
        self.__session = None

    @property
    def session(self) -> ClientSession:
        return self.__session

    async def __aenter__(self) -> "MCPTempClient":
        try:
            session = await MCPSessionManager().connect_to_server(
                server_name="tmp_client",
                exit_stack=self.exit_stack,
                **self.connection_args
            )
            self.__session = session
            return self
        except Exception:
            await self.exit_stack.aclose()
            raise

    async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
    ) -> None:
        await self.exit_stack.aclose()


@singleton
class MCPSessionManager:
    """A manager class to manage different mcp server session."""

    def __init__(self):
        """Init an empty context variable dict and a thread lock used when
        add new key to this dict."""
        self.__mcp_session_dict = ContextVar("__mcp_session_dict__")
        self.__exit_stack = ContextVar("__mcp_exit_stack__")

    def init_session(self):
        self.__exit_stack.set(pick_exit_stack())
        self.__mcp_session_dict.set({})

    @property
    def mcp_session_dict(self) -> dict:
        if not self.__mcp_session_dict.get(None):
            self.__mcp_session_dict.set({})
        return self.__mcp_session_dict.get({})


    @property
    def exit_stack(self) -> AsyncExitStack:
        if not self.__exit_stack.get(None):
            self.__exit_stack.set(pick_exit_stack())
        return self.__exit_stack.get()

    async def clear_session(self):
        await self.exit_stack.aclose()
        self.__exit_stack.set(None)
        self.__mcp_session_dict.set(None)


    def save_mcp_session(self):
        return {
            'mcp_session_dict': self.__mcp_session_dict.get(None),
            'exit_stack': self.__exit_stack.get(None)
        }

    def recover_mcp_session(self, mcp_session_dict, exit_stack):
        self.__mcp_session_dict.set(mcp_session_dict)
        self.__exit_stack.set(exit_stack)

    def get_mcp_server_session_sync(
        self,
        server_name: str,
        transport: Literal["stdio", "sse", "websocket", "streamable_http"] = "stdio",
        **kwargs,
    ) -> ClientSession:
        if self.mcp_session_dict.get(server_name):
            return self.mcp_session_dict.get(server_name)
        return self.connect_to_server_sync(
            server_name=server_name,
            transport=transport,
            **kwargs
        )

    async def get_mcp_server_session(
        self,
        server_name: str,
        transport: Literal["stdio", "sse", "websocket", "streamable_http"] = "stdio",
        **kwargs,
    ) -> ClientSession:
        if self.mcp_session_dict.get(server_name):
            return self.mcp_session_dict.get(server_name)
        return await self.connect_to_server(
            server_name=server_name,
            transport=transport,
            **kwargs
        )

    async def connect_to_server(
        self,
        server_name: str,
        transport: Literal["stdio", "sse", "websocket", "streamable_http"] = "stdio",
        exit_stack: AsyncExitStack = None,
        **kwargs,
    ) -> ClientSession:
        """Connect to an MCP server using either stdio or SSE.
        Args:
            server_name: Name to identify this server connection
            transport: Type of transport to use ("stdio" or "sse"), defaults to "stdio"
            exit_stack: Use a temp exit_stack to get an temp session
            **kwargs: Additional arguments to pass to the specific connection method
        """
        if transport == "sse":
            if "url" not in kwargs:
                raise ValueError("'url' parameter is required for MCP SSE connection")
            session = await self.connect_to_server_via_sse(
                server_name,
                url=kwargs["url"],
                headers=kwargs.get("headers"),
                timeout=kwargs.get("timeout", DEFAULT_HTTP_TIMEOUT),
                sse_read_timeout=kwargs.get("sse_read_timeout", DEFAULT_SSE_READ_TIMEOUT),
                session_kwargs=kwargs.get("session_kwargs"),
                exit_stack=exit_stack
            )
        elif transport == "stdio":
            if "command" not in kwargs:
                raise ValueError("'command' parameter is required for stdio connection")
            if "args" not in kwargs:
                raise ValueError("'args' parameter is required for stdio connection")
            session = await self.connect_to_server_via_stdio(
                server_name,
                command=kwargs["command"],
                args=kwargs["args"],
                env=kwargs.get("env"),
                encoding=kwargs.get("encoding", DEFAULT_ENCODING),
                encoding_error_handler=kwargs.get(
                    "encoding_error_handler", DEFAULT_ENCODING_ERROR_HANDLER
                ),
                session_kwargs=kwargs.get("session_kwargs"),
                exit_stack=exit_stack
            )
        elif transport == "streamable_http":
            if "url" not in kwargs:
                raise ValueError(
                    "'url' parameter is required for MCP streamble_http connection")
            session = await self.connect_to_server_via_streamable_http(
                server_name,
                url=kwargs["url"],
                headers=kwargs.get("headers"),
                timeout=kwargs.get("timeout", DEFAULT_STREAMABLE_HTTP_TIMEOUT),
                sse_read_timeout=kwargs.get("sse_read_timeout",
                                            DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT),
                session_kwargs=kwargs.get("session_kwargs"),
                exit_stack=exit_stack
            )
        elif transport == "websocket":
            if "url" not in kwargs:
                raise ValueError(
                    "'url' parameter is required for MCP websocket connection")
            session = await self.connect_to_server_via_websocket(
                server_name,
                url=kwargs["url"],
                session_kwargs=kwargs.get("session_kwargs"),
            )
        else:
            raise ValueError(f"Unsupported transport: {transport}. Must be 'stdio' or 'sse'")
        return session


    def connect_to_server_sync(
        self,
        server_name: str,
        transport: Literal["stdio", "sse", "websocket", "streamable_http"] = "stdio",
        **kwargs,
    ) -> ClientSession:
        """Connect to an MCP server using either stdio or SSE.
        Args:
            server_name: Name to identify this server connection
            transport: Type of transport to use ("stdio" or "sse"), defaults to "stdio"
            exit_stack: Use a temp exit_stack to get an temp session
            **kwargs: Additional arguments to pass to the specific connection method
        """
        if transport == "sse":
            if "url" not in kwargs:
                raise ValueError("'url' parameter is required for MCP SSE connection")
            session = self.connect_to_server_via_sse_sync(
                server_name,
                url=kwargs["url"],
                headers=kwargs.get("headers"),
                timeout=kwargs.get("timeout", DEFAULT_HTTP_TIMEOUT),
                sse_read_timeout=kwargs.get("sse_read_timeout", DEFAULT_SSE_READ_TIMEOUT),
                session_kwargs=kwargs.get("session_kwargs"),
            )
        elif transport == "stdio":
            if "command" not in kwargs:
                raise ValueError("'command' parameter is required for stdio connection")
            if "args" not in kwargs:
                raise ValueError("'args' parameter is required for stdio connection")
            session = self.connect_to_server_via_stdio_sync(
                server_name,
                command=kwargs["command"],
                args=kwargs["args"],
                env=kwargs.get("env"),
                encoding=kwargs.get("encoding", DEFAULT_ENCODING),
                encoding_error_handler=kwargs.get(
                    "encoding_error_handler", DEFAULT_ENCODING_ERROR_HANDLER
                ),
                session_kwargs=kwargs.get("session_kwargs")
            )
        elif transport == "streamable_http":
            if "url" not in kwargs:
                raise ValueError(
                    "'url' parameter is required for MCP streamble_http connection")
            session = self.connect_to_server_via_streamable_http_sync(
                server_name,
                url=kwargs["url"],
                headers=kwargs.get("headers"),
                timeout=kwargs.get("timeout", DEFAULT_STREAMABLE_HTTP_TIMEOUT),
                sse_read_timeout=kwargs.get("sse_read_timeout",
                                            DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT),
                session_kwargs=kwargs.get("session_kwargs")
            )
        elif transport == "websocket":
            if "url" not in kwargs:
                raise ValueError(
                    "'url' parameter is required for MCP websocket connection")
            session = self.connect_to_server_via_websocket_sync(
                server_name,
                url=kwargs["url"],
                session_kwargs=kwargs.get("session_kwargs"),
            )
        else:
            raise ValueError(f"Unsupported transport: {transport}. Must be 'stdio' or 'sse'")
        return session


    async def connect_to_server_via_stdio(
        self,
        server_name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        encoding: str = DEFAULT_ENCODING,
        encoding_error_handler: Literal[
            "strict", "ignore", "replace"
        ] = DEFAULT_ENCODING_ERROR_HANDLER,
        session_kwargs: dict | None = None,
        exit_stack: AsyncExitStack
    ) -> ClientSession:
        """Connect to a specific MCP server using stdio

        Args:
            server_name: Name to identify this server connection
            command: Command to execute
            args: Arguments for the command
            env: Environment variables for the command
            encoding: Character encoding
            encoding_error_handler: How to handle encoding errors
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        """
        # NOTE: execution commands (e.g., `uvx` / `npx`) require PATH envvar to be set.
        # To address this, we automatically inject existing PATH envvar into the `env` value,
        # if it's not already set.
        env = env or {}
        if "PATH" not in env:
            env["PATH"] = os.environ.get("PATH", "")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
            encoding=encoding,
            encoding_error_handler=encoding_error_handler,
        )

        # Create and store the connection
        activate_exit_stack = exit_stack if exit_stack else self.exit_stack
        stdio_transport = await activate_exit_stack.enter_async_context(stdio_client(server_params))
        read, write = stdio_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            await activate_exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        await session.initialize()
        if not exit_stack:
            self.mcp_session_dict[server_name] = session
        return session

    def connect_to_server_via_stdio_sync(
        self,
        server_name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        encoding: str = DEFAULT_ENCODING,
        encoding_error_handler: Literal[
            "strict", "ignore", "replace"
        ] = DEFAULT_ENCODING_ERROR_HANDLER,
        session_kwargs: dict | None = None
    ) -> ClientSession:
        """Connect to a specific MCP server using stdio

        Args:
            server_name: Name to identify this server connection
            command: Command to execute
            args: Arguments for the command
            env: Environment variables for the command
            encoding: Character encoding
            encoding_error_handler: How to handle encoding errors
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        """
        # NOTE: execution commands (e.g., `uvx` / `npx`) require PATH envvar to be set.
        # To address this, we automatically inject existing PATH envvar into the `env` value,
        # if it's not already set.
        env = env or {}
        if "PATH" not in env:
            env["PATH"] = os.environ.get("PATH", "")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
            encoding=encoding,
            encoding_error_handler=encoding_error_handler,
        )
        stdio_transport = self.exit_stack.enter_async_context(stdio_client(server_params))
        read, write = stdio_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            self.exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        self.run_async(session.initialize)
        self.mcp_session_dict[server_name] = session
        return session

    async def connect_to_server_via_sse(
        self,
        server_name: str,
        *,
        url: str,
        headers: dict | None = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        sse_read_timeout: float = DEFAULT_SSE_READ_TIMEOUT,
        session_kwargs: dict | None = None,
        exit_stack: AsyncExitStack = None
    ) -> ClientSession:
        """Connect to a specific MCP server using SSE

        Args:
            server_name: Name to identify this server connection
            url: URL of the SSE server
            headers: HTTP headers to send to the SSE endpoint
            timeout: HTTP timeout
            sse_read_timeout: SSE read timeout
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        """
        # Create and store the connection
        activate_exit_stack = exit_stack if exit_stack else self.exit_stack
        sse_transport = await activate_exit_stack.enter_async_context(
            sse_client(url, headers, timeout, sse_read_timeout)
        )
        read, write = sse_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            await activate_exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        await session.initialize()
        if not exit_stack:
            self.mcp_session_dict[server_name] = session
        return session

    def connect_to_server_via_sse_sync(
        self,
        server_name: str,
        *,
        url: str,
        headers: dict | None = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        sse_read_timeout: float = DEFAULT_SSE_READ_TIMEOUT,
        session_kwargs: dict | None = None,
    ) -> ClientSession:
        """Connect to a specific MCP server using SSE

        Args:
            server_name: Name to identify this server connection
            url: URL of the SSE server
            headers: HTTP headers to send to the SSE endpoint
            timeout: HTTP timeout
            sse_read_timeout: SSE read timeout
            session_kwargs: Additional keyword arguments to pass to the ClientSession
        """
        # Create and store the connection
        sse_transport = self.exit_stack.enter_async_context(
            sse_client(url, headers, timeout, sse_read_timeout)
        )
        read, write = sse_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            self.exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        self.run_async(session.initialize)
        self.mcp_session_dict[server_name] = session
        return session

    async def connect_to_server_via_streamable_http(
        self,
        server_name: str,
        *,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: timedelta = DEFAULT_STREAMABLE_HTTP_TIMEOUT,
        sse_read_timeout: timedelta = DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT,
        session_kwargs: dict[str, Any] | None = None,
        exit_stack: AsyncExitStack = None
    ) -> ClientSession:
        """Connect to a specific MCP server using Streamable HTTP

        Args:
            server_name: Name to identify this server connection
            url: URL of the endpoint to connect to
            headers: HTTP headers to send to the endpoint
            timeout: HTTP timeout
            sse_read_timeout: How long (in seconds) the client will wait for a new event before disconnecting.
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        """
        # Create and store the connection
        activate_exit_stack = exit_stack if exit_stack else self.exit_stack
        streamable_http_transport = await activate_exit_stack.enter_async_context(
            streamablehttp_client(url, headers, timeout, sse_read_timeout)
        )
        read, write, _ = streamable_http_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            await activate_exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        await session.initialize()
        if not exit_stack:
            self.mcp_session_dict[server_name] = session
        return session

    def connect_to_server_via_streamable_http_sync(
            self,
            server_name: str,
            *,
            url: str,
            headers: dict[str, Any] | None = None,
            timeout: timedelta = DEFAULT_STREAMABLE_HTTP_TIMEOUT,
            sse_read_timeout: timedelta = DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT,
            session_kwargs: dict[str, Any] | None = None
    ) -> ClientSession:
        """Connect to a specific MCP server using Streamable HTTP

        Args:
            server_name: Name to identify this server connection
            url: URL of the endpoint to connect to
            headers: HTTP headers to send to the endpoint
            timeout: HTTP timeout
            sse_read_timeout: How long (in seconds) the client will wait for a new event before disconnecting.
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        """
        # Create and store the connection
        streamable_http_transport = self.exit_stack.enter_async_context(
            streamablehttp_client(url, headers, timeout, sse_read_timeout)
        )
        read, write, _ = streamable_http_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            self.exit_stack.enter_async_context(
                ClientSession(read, write, **session_kwargs)),
        )
        self.run_async(session.initialize)
        self.mcp_session_dict[server_name] = session
        return session

    async def connect_to_server_via_websocket(
        self,
        server_name: str,
        *,
        url: str,
        session_kwargs: dict[str, Any] | None = None,
        exit_stack: AsyncExitStack = None
    ) -> ClientSession:
        """Connect to a specific MCP server using Websockets

        Args:
            server_name: Name to identify this server connection
            url: URL of the Websocket endpoint
            session_kwargs: Additional keyword arguments to pass to the ClientSession
            exit_stack: Use a temp exit_stack to get an temp session
        Raises:
            ImportError: If websockets package is not installed
        """
        activate_exit_stack = exit_stack if exit_stack else self.exit_stack
        try:
            from mcp.client.websocket import websocket_client
        except ImportError:
            raise ImportError(
                "Could not import websocket_client. ",
                "To use Websocket connections, please install the required dependency with: ",
                "'pip install mcp[ws]' or 'pip install websockets'",
            ) from None

        ws_transport = await activate_exit_stack.enter_async_context(websocket_client(url))
        read, write = ws_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            await activate_exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        await session.initialize()
        if not exit_stack:
            self.mcp_session_dict[server_name] = session
        return session

    def connect_to_server_via_websocket_sync(
        self,
        server_name: str,
        *,
        url: str,
        session_kwargs: dict[str, Any] | None = None
    ) -> ClientSession:
        """Connect to a specific MCP server using Websockets

        Args:
            server_name: Name to identify this server connection
            url: URL of the Websocket endpoint
            session_kwargs: Additional keyword arguments to pass to the ClientSession
        Raises:
            ImportError: If websockets package is not installed
        """
        try:
            from mcp.client.websocket import websocket_client
        except ImportError:
            raise ImportError(
                "Could not import websocket_client. ",
                "To use Websocket connections, please install the required dependency with: ",
                "'pip install mcp[ws]' or 'pip install websockets'",
            ) from None

        ws_transport = self.exit_stack.enter_async_context(websocket_client(url))
        read, write = ws_transport
        session_kwargs = session_kwargs or {}
        session = cast(
            ClientSession,
            self.exit_stack.enter_async_context(ClientSession(read, write, **session_kwargs)),
        )

        self.run_async(session.initialize)
        self.mcp_session_dict[server_name] = session
        return session

    def safe_close_stack(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if isinstance(self.exit_stack, SyncAsyncExitStack):
                self.exit_stack.close()
            elif isinstance(self.exit_stack, AsyncExitStack):
                asyncio.run(self.exit_stack.aclose())
        else:
            if isinstance(self.exit_stack, AsyncExitStack):
                loop.create_task(self.exit_stack.aclose())
            elif isinstance(self.exit_stack, SyncAsyncExitStack):
                self.exit_stack.close()
        self.__exit_stack.set(None)
        self.__mcp_session_dict.set(None)

    def run_async(self, func, *args, **kwargs):
        return self.exit_stack.run_async(func, *args, **kwargs)
