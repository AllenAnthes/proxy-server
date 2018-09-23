from asyncio import StreamReader, StreamWriter
from collections import defaultdict
from typing import Optional
import logging
import asyncio
import socket
import re
import sys

# pre-compile regex patterns to be used to parse requests
request_pattern = re.compile("^(?P<method>GET|HEAD|POST) (?P<resource>.+?) ")
host_pattern = re.compile("Host: (?P<host>.*?)\r\n")

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
        loop.run_forever()

    async def handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        """
        Called by the server whenever a new client connection is received.

        :param reader: StreamReader to receive data from the client
        :param writer: StreamWriter to write data back to the client
        """

        # read the first 1024 bytes of the request so we can parse
        # method, requested resource, etc.
        raw_data = await reader.read(1024)
        data = raw_data.decode()
        method_matches = request_pattern.search(data)

        host_info = writer.get_extra_info('peername')
        logger.info(f"[*] Incoming request from {host_info} [*]")

        # Drop connects if the method can't be parsed or if
        # the client attempts a secure connection
        if not method_matches:
            logger.info('[*] Invalid request method. Dropping request. [*]')
            return

        # Parse/decode the method and actual requested resource
        # from the raw request bytes
        method = method_matches.group('method')
        request = method_matches.group('resource')
        logger.info(f'[*] Method: {method} [*]')
        logger.info(f'[*] Parsed request: {request} [*]')

        # If it's a GET request check if we can serve it from cache
        if method == 'GET' and request is not None:
            cache_result = self._check_cache(request)
            logger.info(f'[*] Cache hit: {bool(cache_result)} [*]')
            if cache_result:
                logger.info('[*] Returning result from cache [*]')
                writer.write(cache_result)
                return

        host = host_pattern.search(data).group('host')
        remote_host = self._get_remote_host(host)
        logger.info(f"[*] Forwarding {host_info} -> {remote_host} [*]")

        if remote_host is None:
            logger.info(f'[*] Could not resolve host for {host} [*]')
            return

        try:
            remote_reader, remote_writer = await asyncio.open_connection(remote_host, 80)

            # send the data from the initial request
            remote_writer.write(raw_data)
            await writer.drain()

            # Create the pipes in both directions
            requests = self._forward(reader, remote_writer)
            responses = self._forward(remote_reader, writer, request)

            # Wait until both pipes are finished
            # Using asyncio.gather allows us to run them in parallel
            await asyncio.gather(requests, responses)

        except socket.gaierror:
            logger.info(f'[!] Error on attempting to open remote host {remote_host} [!]')

        finally:
            writer.close()

    @classmethod
    async def _forward(cls, reader: StreamReader, writer: StreamWriter, request=None) -> None:
        """
        :param reader: StreamReader to receive data from
        :param writer: Stream writer to write data to
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

    @classmethod
    def _get_remote_host(cls, host: str) -> Optional[str]:
        """
        Utility method parses the hostname from the request and queries DNS for
        its IP address

        :param host: Host to query DNS for
        :return: The host's IP adress if it can be resolved, else None
        """
        try:
            host = socket.gethostbyname(host)
            return host
        except socket.gaierror:
            return None

    @classmethod
    def _check_cache(cls, request: str) -> Optional[bytes]:
        """
        Returns the requested resource from the server's cache
        if it was previously requested.

        Current implementation doesn't include any functionality for removing
        stale results.

        :param request: The URL of the requested resource
        :return: The resource if found, else None
        """
        return cls.cache.get(request, None)


if __name__ == '__main__':
    proxy_server = ProxyServer('127.0.0.1', 8000)
    proxy_server.serve()
