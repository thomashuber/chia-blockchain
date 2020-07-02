from dataclasses import dataclass

from src.util.ints import uint16
from src.util.streamable import Streamable, streamable

import ipaddress


@dataclass(frozen=True)
@streamable
class PeerInfo(Streamable):
    # TODO: Change `host` type to bytes16
    host: str
    port: uint16

    def get_key(self):
        try:
            ip = ipaddress.IPv6Address(self.host)
        except ValueError:
            ip_v4 = ipaddress.IPv4Address(self.host)
            ip = ipaddress.IPv6Address(
                int(ipaddress.IPv6Address("2002::"))
                | (int(ip_v4) << 80)
            )
        key = ip.packed
        key += bytes(
            [
                self.port // 0x100,
                self.port & 0x0FF,
            ]
        )
        return key

    def get_group(self):
        # TODO: Port everything from Bitcoin.
        ipv4 = 1
        try:
            ip = ipaddress.IPv4Address(
                self.host
            )
        except ValueError:
            ip = ipaddress.IPv6Address(
                self.host
            )
            ipv4 = 0
        group = bytes([ipv4]) + ip.packed[:2]
        return group
    
    def __eq__(self, other):
        return (self.host == other.host and self.port == other.port)
