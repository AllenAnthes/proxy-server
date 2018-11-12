from asyncio import StreamReader, StreamWriter, coroutine
from socket import AF_INET, IPPROTO_TCP
from collections import defaultdict
from typing import Tuple
import logging
import asyncio
import re
import sys

# pre-compile regex pattern to be used to parse requests
# contains named capture groups for the request method, requested resource URL,
# and the value of the Host header
request_pattern = re.compile("^(?P<method>GET|HEAD|POST) (?P<resource>.+?) [\s\S]*?Host: (?P<host>.*?)\r\n")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('server')


class ProxyServer:
    cache = defaultdict(bytes)

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def serve(self) -> None:
        """
        Starts the server and serves requests until terminated
        """
        loop = asyncio.get_event_loop()
        coroutine = asyncio.start_server(self.handle_client, self.host, self.port)
        server = loop.run_until_complete(coroutine)
        logger.info(f'Serving on {server.sockets[0].getsockname()}')
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info('[*] KeyboardInterrupt detected -- Stopping server [*]')

    async def handle_client(self, local_reader: StreamReader, local_writer: StreamWriter) -> None:
        """
        Called by the server whenever a new client connection is received.

        :param local_reader: StreamReader to receive data from the client
        :param local_writer: StreamWriter to write data back to the client
        """
        try:

            host_info = local_writer.get_extra_info('peername')
            logger.info(f"[*] Incoming request from {host_info} [*]")

            # read the first 1024 bytes of the request so we can parse
            # method, requested resource, etc.
            raw_data = await local_reader.read(1024)
            method, requested_resource, host = await self._parse_request(raw_data)

            cache_hit = self._check_cache(method, requested_resource, local_writer)
            if cache_hit:
                return

            logger.info(f"[*] Forwarding {host_info} -> {host} [*]")
            to_webserver, to_client = await self._open_remote_connection(host, local_reader, local_writer,
                                                                         raw_data, requested_resource)
            # Wait until both pipes are finished. Using gather allows
            # us to start both processes and run them in parallel
            await asyncio.gather(to_webserver, to_client)

        except InvalidRequestMethod:
            logger.info('[*] Invalid request method. Dropping request. [*]')

        finally:
            local_writer.close()

    @classmethod
    async def _parse_request(cls, raw_data: bytes) -> Tuple[str, str, str]:
        """
        Decodes the raw request bytes to a string and parses the result with regex
        to pull out necessary values

        :param raw_data: Bytes containing the client's request
        :return: The request method, requested resource, and the remote hostname
        """
        try:
            data = raw_data.decode()
        except UnicodeDecodeError:
            raise InvalidRequestMethod()
        matches = request_pattern.search(data)

        # Drop connection if the method can't be parsed or if it's unsupported
        if not matches:
            raise InvalidRequestMethod()

        method, resource, host = matches.groupdict().values()
        logger.info(f'[*] Method: {method} | Requested resource: {resource} [*]')

        return method, resource, host

    @classmethod
    def _check_cache(cls, method: str, requested_resource: str, writer: StreamWriter) -> bool:
        """
        Returns the requested resource from the server's cache
        if it was previously requested.

        Current implementation doesn't include any functionality for removing
        stale results.

        :param method: Request method
        :param requested_resource: URL of the requested resource
        :param writer: StreamWriter for the local socket connection
        :return: True if it's a cache hit, else False
        """
        if method == 'GET':
            cache_result = cls.cache.get(requested_resource, None)
            logger.info(f'[*] Cache hit: {bool(cache_result)} [*]')
            if cache_result:
                logger.info('[*] Returning result from cache [*]')
                writer.write(cache_result)
                writer.close()
                return True
        return False

    @classmethod
    async def _open_remote_connection(cls, host: str, local_reader: StreamReader, local_writer: StreamWriter,
                                      raw_data: bytes, requested_resource: str) -> Tuple[coroutine, coroutine]:
        """
        Open a TCP socket with the remote web server as well
        as pipelines to push data in both directions

        :param host: Remote hostname
        :param local_reader: StreamReader for the local socket connection
        :param local_writer: StreamWriter for the local socket connection
        :param raw_data: The first 1024 (or less) bytes of the request
        :param requested_resource: URL of the requested resource
        :return to_webserver, to_client: Tuple of the two socket connections as
                                            coroutines to be run asynchronously
        """

        remote_reader, remote_writer = await asyncio.open_connection(host, 80, family=AF_INET, proto=IPPROTO_TCP)

        # send the data from the initial request
        remote_writer.write(raw_data)
        await local_writer.drain()

        # Create the pipes in both directions
        to_webserver = cls._forward(local_reader, remote_writer)
        to_client = cls._forward(remote_reader, local_writer, requested_resource)
        return to_webserver, to_client

    @classmethod
    async def _forward(cls, reader: StreamReader, writer: StreamWriter, request=None) -> None:
        """
        Pipes the data read from the StreamReader to the StreamWriter.

        If the request URL is provided the data will be cached before it is written
        back to the client.

        :param reader: StreamReader to receive data from
        :param writer: StreamWriter to write data to
        :param request: Optionally provided request URL.  If present the
                        returned data will be cached with the URL as the key.
        """
        try:
            while not reader.at_eof():
                data = await reader.read(1024)
                if request is not None:
                    cls.cache[request] += data
                writer.write(data)
                await writer.drain()
        except Exception as e:
            logger.exception(f'[!] Exception during forwarding {e} [!]')
        finally:
            writer.close()


class InvalidRequestMethod(Exception):
    pass


if __name__ == '__main__':
    proxy_server = ProxyServer('127.0.0.1', 8000)
    proxy_server.serve()
