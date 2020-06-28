import asyncio
import logging
import logging.config
import signal

from typing import Any, AsyncGenerator, Callable, List, Optional, Tuple

try:
    import uvloop
except ImportError:
    uvloop = None

from src.protocols import introducer_protocol
from src.server.outbound_message import Delivery, Message, NodeType, OutboundMessage
from src.server.server import ChiaServer, start_server
from src.types.peer_info import PeerInfo
from src.util.logging import initialize_logging
from src.util.config import load_config_cli, load_config
from src.util.setproctitle import setproctitle
from random import randrange
from math import log
from .reconnect_task import start_reconnect_task

OutboundMessageGenerator = AsyncGenerator[OutboundMessage, None]


def create_periodic_introducer_poll_task(
    server,
    peer_info,
    global_connections,
    introducer_connect_interval,
    target_outbound_connections,
):
    """

    Start a background task connecting periodically to the introducer and
    requesting the peer list.
    """

    def poisson_next_send(now, avg_interval_seconds):
        return now + (
            math.log(
                random.randrange(1 << 48) * -0.0000000000000035527136788 + 1
            ) * avg_interval_seconds * -1000000.0 + 0.5
        )


    def _num_needed_peers() -> int:
        diff = target_outgoing_connections - global_connections.count_outbound_connections()
        return diff if diff >= 0 else 0

    async def introducer_client():
        async def on_connect() -> OutboundMessageGenerator:
            msg = Message("request_peers", introducer_protocol.RequestPeers())
            yield OutboundMessage(NodeType.INTRODUCER, msg, Delivery.RESPOND)

        # If we are still connected to introducer, disconnect
        for connection in global_connections.get_connections():
            if connection.connection_type == NodeType.INTRODUCER:
                global_connections.close(connection)
        # The first time connecting to introducer, keep trying to connect
        if _num_needed_peers():
            if not await server.start_client(peer_info, on_connect):
                await asyncio.sleep(5)
                continue
    
    async def connect_to_peers():
        next_feeler = poisson_next_send(time.time()*1000*1000, 120)
        while True:
            # We don't know any address, connect to the introducer to get some.
            if global_connections.size() == 0:
                await introducer_client()
                continue
            # Only connect out to one peer per network group (/16 for IPv4).
            groups = []
            for peer in global_connections.peers.get_peers():
                group = peer.get_group()
                if group not in groups:
                    groups.append(group)
            count_outbound = global_connections.count_outbound_connections()

            # Feeler Connections
            #
            # Design goals:
            # * Increase the number of connectable addresses in the tried table.
            #
            # Method:
            # * Choose a random address from new and attempt to connect to it if we can connect
            # successfully it is added to tried.
            # * Start attempting feeler connections only after node finishes making outbound
            # connections.
            # * Only make a feeler connection once every few minutes.

            is_feeler = False

            if count_outbound >= target_outbound_connections:
                if time.time() * 1000 * 1000 > next_feeler:
                    next_feeler = poisson_next_send(time.time() * 1000 * 1000, 120)                    
                    is_feeler = True
                else:
                    continue
            
            address_manager = global_connections.peers.address_manager
            await address_manager.resolve_tried_collisions()
            tries = 0
            now = time.time()
            addr = None
            while True:
                addr = address_manager.select_tried_collision()
                if (
                    not is_feeler
                    or addr is None
                ):
                    addr = await address_manager.select_peer(is_feeler)
                if addr is None:
                    break
                # Require outbound connections, other than feelers, to be to distinct network groups.
                if (
                    not is_feeler
                    and addr.get_group() in groups
                ):
                    break
                tries += 1
                if tries > 100:
                    break
                # only consider very recently tried nodes after 30 failed attempts
                if (
                    now - addr.nLastTry < 600
                    and tries < 30
                ):
                    continue

            disconnect_after_handshake = is_feeler
            if count_outbound >= target_outbound_connections:
                disconnect_after_handshake = True
            if addr is not None:
                asyncio.create_task(self.server.start_client(addr, None, None, disconnect_after_handshake))
            await asnycio.sleep(5)

    return asyncio.create_task(connect_to_peers())


class Service:
    def __init__(
        self,
        root_path,
        api: Any,
        node_type: NodeType,
        advertised_port: int,
        service_name: str,
        server_listen_ports: List[int] = [],
        connect_peers: List[PeerInfo] = [],
        on_connect_callback: Optional[OutboundMessage] = None,
        rpc_start_callback_port: Optional[Tuple[Callable, int]] = None,
        start_callback: Optional[Callable] = None,
        stop_callback: Optional[Callable] = None,
        await_closed_callback: Optional[Callable] = None,
        periodic_introducer_poll: Optional[Tuple[PeerInfo, int, int]] = None,
    ):
        net_config = load_config(root_path, "config.yaml")
        ping_interval = net_config.get("ping_interval")
        network_id = net_config.get("network_id")
        assert ping_interval is not None
        assert network_id is not None

        self._node_type = node_type

        proctitle_name = f"chia_{service_name}"
        setproctitle(proctitle_name)
        self._log = logging.getLogger(service_name)

        config = load_config_cli(root_path, "config.yaml", service_name)
        initialize_logging(f"{service_name:<30s}", config["logging"], root_path)

        self._rpc_start_callback_port = rpc_start_callback_port

        self._server = ChiaServer(
            config["port"],
            api,
            node_type,
            ping_interval,
            network_id,
            root_path,
            config,
        )
        for _ in ["set_server", "_set_server"]:
            f = getattr(api, _, None)
            if f:
                f(self._server)

        self._connect_peers = connect_peers
        self._server_listen_ports = server_listen_ports

        self._api = api
        self._task = None
        self._is_stopping = False

        self._periodic_introducer_poll = periodic_introducer_poll
        self._on_connect_callback = on_connect_callback
        self._start_callback = start_callback
        self._stop_callback = stop_callback
        self._await_closed_callback = await_closed_callback

    def start(self):
        if self._task is not None:
            return

        async def _run():
            if self._start_callback:
                await self._start_callback()

            self._introducer_poll_task = None
            if self._periodic_introducer_poll:
                (
                    peer_info,
                    introducer_connect_interval,
                    target_peer_count,
                ) = self._periodic_introducer_poll

                self._introducer_poll_task = create_periodic_introducer_poll_task(
                    self._server,
                    peer_info,
                    self._server.global_connections,
                    introducer_connect_interval,
                    target_peer_count,
                )

            self._rpc_task = None
            if self._rpc_start_callback_port:
                rpc_f, rpc_port = self._rpc_start_callback_port
                self._rpc_task = asyncio.ensure_future(
                    rpc_f(self._api, self.stop, rpc_port)
                )

            self._reconnect_tasks = [
                start_reconnect_task(self._server, _, self._log)
                for _ in self._connect_peers
            ]
            self._server_sockets = [
                await start_server(self._server, self._on_connect_callback)
                for _ in self._server_listen_ports
            ]

            try:
                asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self.stop)
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.stop)
            except NotImplementedError:
                self._log.info("signal handlers unsupported")

            for _ in self._server_sockets:
                await _.wait_closed()

            await self._server.await_closed()
            if self._await_closed_callback:
                await self._await_closed_callback()

        self._task = asyncio.ensure_future(_run())

    async def run(self):
        self.start()
        await self.wait_closed()
        self._log.info("Closed all node servers.")
        return 0

    def stop(self):
        if not self._is_stopping:
            self._is_stopping = True
            for _ in self._server_sockets:
                _.close()
            for _ in self._reconnect_tasks:
                _.cancel()
            self._server.close_all()
            self._api._shut_down = True
            if self._introducer_poll_task:
                self._introducer_poll_task.cancel()
            if self._stop_callback:
                self._stop_callback()

    async def wait_closed(self):
        await self._task
        if self._rpc_task:
            await self._rpc_task
            self._log.info("Closed RPC server.")
        self._log.info("%s fully closed", self._node_type)


async def async_run_service(*args, **kwargs):
    service = Service(*args, **kwargs)
    return await service.run()


def run_service(*args, **kwargs):
    if uvloop is not None:
        uvloop.install()
    # TODO: use asyncio.run instead
    # for now, we use `run_until_complete` as `asyncio.run` blocks on RPC server not exiting
    if 1:
        return asyncio.get_event_loop().run_until_complete(
            async_run_service(*args, **kwargs)
        )
    else:
        return asyncio.run(async_run_service(*args, **kwargs))
