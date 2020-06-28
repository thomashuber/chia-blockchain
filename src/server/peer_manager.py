from src.util.hash import std_hash
from secrets import randbits
from random import randrange, choice
from src.types.peer_info import PeerInfo

import time
import asyncio
import math

TRIED_BUCKETS_PER_GROUP = 8
NEW_BUCKETS_PER_SOURCE_GROUP = 64
TRIED_BUCKET_COUNT = 256
NEW_BUCKET_COUNT = 1024
BUCKET_SIZE = 64
TRIED_COLLISION_SIZE = 10
NEW_BUCKETS_PER_ADDRESS = 8


# This is a Python port from 'CAddrInfo' class from Bitcoin core code.
class ExtendedPeerInfo:
    def __init__(
        self,
        peer_info,
        src_peer,
    ):
        self.peer_info = peer_info
        self.src = src_peer
        self.last_try = None
        self.random_pos = None
        self.is_tried = False
        self.ref_count = 0
        self.nLastSuccess = 0
        self.nLastTry = 0
        self.nLastCountAttempt = 0

    def get_tried_bucket(self, nKey):
        hash1 = int.from_bytes(
            bytes(
                std_hash(nKey + self.peer_info.get_key())[:8]
            )
        )
        hash1 = hash1 % TRIED_BUCKETS_PER_GROUP
        hash2 = int.from_bytes(
            bytes(
                std_hash(
                    nKey
                    + self.peer_info.get_group()
                    + bytes([hash1])
                )[:8]
            )
        )
        return hash2 % TRIED_BUCKET_COUNT

    def get_new_bucket(self, nKey, src_peer=None):
        if src_peer is None:
            src_peer = self.src
        hash1 = int.from_bytes(
            bytes(
                std_hash(
                    nKey
                    + self.peer_info.get_group()
                    + src_peer.get_group()
                )[:8]
            )
        )
        hash1 = hash1 % NEW_BUCKETS_PER_SOURCE_GROUP
        hash2 = int.from_bytes(
            bytes(
                std_hash(
                    nKey
                    + src_peer.get_group()
                    + bytes([hash1])
                )[:8]
            )
        )
        return hash2 % NEW_BUCKET_COUNT

    def get_bucket_position(self, nKey, fNew, nBucket):
        ch = 'N' if fNew else 'K'
        hash1 = int.from_bytes(
            bytes(
                nKey
                + bytes([ch, nBucket])
                + self.peer_info.get_key()
            )[:8]
        )
        return hash1 % BUCKET_SIZE

    def is_terrible(self, nNow):
        if (
            self.last_try is not None
            and self.last_try >= nNow - 60
        ):
            return False

    def get_selection_chance(self, nNow):
        fChance = 1.0
        nSinceLastTry = max(self.nNow - self.last_try, 0)
        # deprioritize very recent attempts away
        if nSinceLastTry < 60 * 10:
            fChance *= 0.01

        # deprioritize 66% after each failed attempt,
        # but at most 1/28th to avoid the search taking forever or overly penalizing outages.
        fChance *= pow(0.66, min(self.nAttempts, 8))
        return fChance

    def is_valid(self):
        raise RuntimeError("Not implemented.")


# This is a Python port from 'CAddrMan' class from Bitcoin core code.
class AddressManager:
    def __init__(self):
        self.id_count = 0
        self.nKey = randbits(256)
        self.random_pos = []
        self.tried_matrix = [
            [
                -1 for x in range(TRIED_BUCKET_COUNT)
            ]
            for y in range(BUCKET_SIZE)
        ]
        self.new_matrix = [
            [
                -1 for x in range(NEW_BUCKET_COUNT)
            ]
            for y in range(BUCKET_SIZE)
        ]
        self.tried_count = 0
        self.new_count = 0
        self.nLastGood = 1
        self.lock = asyncio.Lock()

    def create_(self, addr: PeerInfo, addr_src: PeerInfo):
        self.id_count += 1
        node_id = self.id_count
        self.map_info[node_id] = ExtendedPeerInfo(addr, addr_src)
        self.map_addr[addr] = node_id
        self.map_info[node_id].random_pos = len(self.random_pos)
        self.random_pos.append(node_id)
        return (self.map_info[node_id], node_id)

    def find_(self, addr: PeerInfo):
        if addr not in self.map_addr:
            return (None, None)
        node_id = self.map_addr[addr]
        if node_id not in self.map_info:
            return (None, node_id)
        return (self.map_info[node_id], node_id)

    def swap_random_(self, rand_pos_1, rand_pos_2):
        if rand_pos_1 == rand_pos_2:
            return
        assert(rand_pos_1 < len(self.random_pos) and rand_pos_2 < len(self.random_pos))
        node_id_1 = self.random_pos[rand_pos_1]
        node_id_2 = self.random_pos[rand_pos_2]
        self.map_info[node_id_1].random_pos = rand_pos_2
        self.map_info[node_id_2].random_pos = rand_pos_1
        self.random_pos[rand_pos_1] = node_id_2
        self.random_pos[rand_pos_2] = node_id_1

    def make_tried_(self, info, node_id):
        for bucket in range(NEW_BUCKET_COUNT):
            pos = info.get_bucket_position(self.nKey, True, bucket)
            if self.tried_matrix[bucket][pos] == node_id:
                self.tried_matrix[bucket][pos] -= 1
                info.ref_count -= 1
        assert(info.ref_count == 0)
        self.count_new -= 1
        cur_bucket = info.get_tried_bucket(self.nKey)
        cur_bucket_pos = info.get_bucket_position(self.nKey, False, cur_bucket)
        if self.tried_matrix[cur_bucket][cur_bucket_pos] != -1:
            # Evict the old node from the tried table.
            node_id_evict = self.tried_matrix[cur_bucket][cur_bucket_pos]
            assert node_id_evict in self.map_info
            old_info = self.map_info[node_id_evict]
            old_info.is_tried = False
            self.tried_matrix[cur_bucket][cur_bucket_pos] = -1
            self.tried_count -= 1
            # Find its position into new table.
            new_bucket = old_info.get_new_bucket(self.nKey)
            new_bucket_pos = old_info.get_bucket_position(self.nKey, True, new_bucket)
            self.clear_new_(new_bucket, new_bucket_pos)
            old_info.ref_count = 1
            self.new_matrix[new_bucket][new_bucket_pos] = node_id_evict
            self.new_count += 1
        self.tried_matrix[cur_bucket][cur_bucket_pos] = node_id
        self.tried_count += 1
        info.is_tried = True

    def clear_new_(self, bucket, pos):
        if self.new_matrix[bucket][pos] != -1:
            delete_id = self.new_matrix[bucket][pos]
            delete_info = self.map_info[delete_id]
            assert delete_info.ref_count > 0
            delete_info.ref_count -= 1
            self.new_matrix[bucket][pos] = -1
            if delete_info.ref_count == 0:
                self.delete_new_entry_(delete_id)

    def mark_good_(self, addr, test_before_evict, nTime):
        self.nLastGood = self.nTime
        (info, node_id) = self.find_(addr)
        if info is None:
            return

        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        # update info
        info.nLastSuccess = self.nTime
        info.nLastTry = self.nTime
        info.nAttempts = 0
        # nTime is not updated here, to avoid leaking information about
        # currently-connected peers.

        # if it is already in the tried set, don't do anything else
        if info.is_tried:
            return

        # find a bucket it is in now
        nRnd = randrange(NEW_BUCKET_COUNT)
        nUBucket = -1
        for n in range(NEW_BUCKET_COUNT):
            nB = (n + nRnd) % NEW_BUCKET_COUNT
            nBpos = info.get_bucket_position(self.nKey, True, nB)
            if self.new_matrix[nB][nBpos] == node_id:
                nUBucket = nB
                break

        # if no bucket is found, something bad happened;
        if nUBucket == -1:
            return

        # NOTE(Florin): Double check this. It's not used anywhere else.

        # which tried bucket to move the entry to
        tried_bucket = info.GetTriedBucket(self.nKey)
        tried_bucket_pos = info.get_bucket_position(self.nKey, False, tried_bucket)

        # Will moving this address into tried evict another entry?
        if (test_before_evict and self.tried_matrix[tried_bucket][tried_bucket_pos] != -1):
            if len(self.tried_collisions) < TRIED_COLLISION_SIZE:
                self.tried_collisions.insert(node_id)
        else:
            self.make_tried_(info, node_id)

    def delete_new_entry_(self, node_id):
        info = self.map_info[node_id]
        self.swap_random_(info.random_pos, len(self.random_pos) - 1)
        self.random_pos = self.random_pos[:-1]
        self.map_addr.erase(info)
        self.map_info.erase(node_id)
        self.new_count -= 1

    def add_to_new_table_(self, addr, source, nTimePenalty):
        fNew = False
        (info, node_id) = self.find_(addr)
        if (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            nTimePenalty = 0

        if info is not None:
            # periodically update nTime
            currently_online = time.time() - addr.nTime < 24 * 60 * 60
            update_interval = 60 * 60 if currently_online else 24 * 60 * 60
            if (
                addr.nTime is not None
                and (
                    info.nTime is None
                    or info.nTime < addr.nTime - update_interval - nTimePenalty
                )
            ):
                info.nTime = max(0, addr.nTime - nTimePenalty)

            # add services
            # pinfo->nServices = ServiceFlags(pinfo->nServices | addr.nServices);

            # do not update if no new information is present
            if (
                addr.nTime is None
                or (
                    info.nTime is not None
                    and addr.nTime <= info.nTime
                )
            ):
                return False

            # do not update if the entry was already in the "tried" table
            if info.is_tried:
                return False

            # do not update if the max reference count is reached
            if info.nRefCount == NEW_BUCKETS_PER_ADDRESS:
                return False

            # stochastic test: previous nRefCount == N: 2^N times harder to increase it
            factor = (1 << info.nRefCount)
            if (
                factor > 1
                and randrange(factor) != 0
            ):
                return False
        else:
            (info, node_id) = self.create_(addr, source)
            info.nTime = max(0, info.nTime - nTimePenalty)
            self.nNew += 1
            self.fNew = True

        nUBucket = info.get_new_bucket(self.nKey, source)
        nUBucketPos = info.get_bucket_position(self.nKey, True, nUBucket)
        if self.new_matrix[nUBucket][nUBucketPos] != node_id:
            fInsert = (self.new_matrix[nUBucket][nUBucketPos] == -1)
            if not fInsert:
                info_existing = self.map_info[
                    self.new_maxtrix[nUBucket][nUBucketPos]
                ]
                if (info_existing.IsTerrible() or (info_existing.nRefCount > 1 and info.nRefCount == 0)):
                    fInsert = True
            if fInsert:
                self.clear_new(nUBucket, nUBucketPos)
                info.nRefCount += 1
                self.new_matrix[nUBucket][nUBucketPos] = node_id
            else:
                if info.nRefCount == 0:
                    self.delete_new_entry_(node_id)
        return fNew

    def attempt_(self, addr, count_failures, nTime):
        info, _ = self.find_(addr)
        if info is not None:
            return

        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        info.nLastTry = nTime
        if (count_failures and info.nLastCountAttempt < info.nLastGood):
            info.nLastCountAttempt = nTime
            info.nAttempts += 1

    def select_peer_(self, new_only):
        if len(self.random_pos) == 0:
            return None

        if (new_only and self.count_new == 0):
            return None

        # Use a 50% chance for choosing between tried and new table entries.
        if (
            not new_only
            and self.tried_count > 0
            and (
                self.new_count == 0
                or randrange(2) == 0
            )
        ):
            fChanceFactor = 1.0
            while True:
                nKBucket = randrange(TRIED_BUCKET_COUNT)
                nKBucketPos = randrange(BUCKET_SIZE)
                while self.tried_matrix[nKBucket][nKBucketPos] == -1:
                    nKBucket = (nKBucket + randbits(math.log2(TRIED_BUCKET_COUNT))) % TRIED_BUCKET_COUNT
                    nKBucketPos = (nKBucketPos + randbits(math.log2(BUCKET_SIZE))) % BUCKET_SIZE
                node_id = self.tried_matrix[nKBucket][nKBucketPos]
                (info, _) = self.map_info[node_id]
                if randbits(30) < (fChanceFactor * info.GetChance() * (1 << 30)):
                    return info
                fChanceFactor *= 1.2
        else:
            fChanceFactor = 1.0
            while True:
                nUBucket = randrange(NEW_BUCKET_COUNT)
                nUBucketPos = randrange(BUCKET_SIZE)
                while self.new_table[nUBucket][nUBucketPos] == -1:
                    nUBucket = (nUBucket + randbits(math.log2(NEW_BUCKET_COUNT))) % NEW_BUCKET_COUNT
                    nUBucketPos = (nUBucketPos + randbits(math.log2(BUCKET_SIZE))) % BUCKET_SIZE
                node_id = self.new_table[nUBucket][nUBucketPos]
                info = self.map_info[node_id]
                if (randbits(30) < fChanceFactor * info.GetChance() * (1 << 30)):
                    return info
                fChanceFactor *= 1.2

    def resolve_tried_collisions_(self):
        for node_id in self.tried_collisions[:]:
            resolved = False
            if node_id not in self.map_info:
                resolved = True
            else:
                info = self.map_info[node_id]
                tried_bucket = info.get_tried_bucket(self.nKey)
                tried_bucket_pos = info.get_tried_bucket(self.nKey, False, tried_bucket)
                if not info.is_valid():
                    resolved = True
                elif self.tried_matrix[tried_bucket][tried_bucket_pos] != -1:
                    old_id = self.tried_matrix[tried_bucket][tried_bucket_pos]
                    old_info = self.map_addr[old_id]
                    if time.time() - old_info.nLastSuccess < 4 * 60 * 60:
                        resolved = True
                    elif time.time() - old_info.nLastTry < 4 * 60 * 60:
                        if time.time() - old_info.nLastTry > 60:
                            self.mark_good_(info, False, time.time())
                            resolved = True
                    elif time.time() - info.nLastSuccess > 40 * 60:
                        self.mark_good_(info, False, time.time())
                        resolved = True
                else:
                    self.mark_good_(info, False, time.time())
                    resolved = True
            if resolved:
                self.tried_collisions.remove(node_id)

    def select_tried_collision_(self):
        if len(self.tried_collisions) == 0:
            return None
        new_id = choice(self.tried_collisions)
        if new_id not in self.map_info:
            self.tried_collisions.remove(new_id)
            return None
        new_info = self.map_info[new_id]
        tried_bucket = new_info.get_tried_bucket(self.nKey)
        tried_bucket_pos = new_info.get_bucket_position(self.nKey, False, tried_bucket)

        old_id = self.tried_matrix[tried_bucket][tried_bucket_pos]
        return self.map_info[old_id]

    def get_peers_(self):
        addr = []
        num_nodes = 23 * len(self.random_pos) / 100
        if num_nodes > 2500:
            num_nodes = 2500

        for n in range(len(self.random_pos)):
            if len(addr) > num_nodes:
                return addr

            nRndPos = randrange(len(self.random_pos) - n) + n
            self.swap_random_(n, nRndPos)
            info = self.map_info[self.random_pos[n]]
            if not info.is_terrible():
                addr.append(info)

        return addr

    def connect_(self, addr, nTime):
        info, _ = self.find_(addr)
        if info is None:
            return

        # check whether we are talking about the exact same peer
        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        update_interval = 20 * 60
        if nTime - info.nTime > update_interval:
            info.nTime = nTime

    async def mark_connected_(self, addr):
        info, _ = self.find_(addr)
        if info is None:
            return

        # check whether we are talking about the exact same CService (including same port)
        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        # update info
        update_interval = 20 * 60
        if self.nTime - info.nTime > update_interval:
            info.nTime = self.nTime

    async def add_to_new_table(self, addresses, source, penalty=0):
        async with self.lock:
            for addr in addresses:
                self.add_to_new_table_(addr, source, penalty)

    # Mark an entry as accesible.
    async def mark_good(self, addr, test_before_evict, nTime=time.time()):
        async with self.lock:
            self.mark_good_(addr, test_before_evict, nTime)

    # Mark an entry as connection attempted to.
    async def attempt(self, addr, count_failures, nTime=time.time()):
        async with self.lock:
            self.attempt_(addr, count_failures, nTime)

    # See if any to-be-evicted tried table entries have been tested and if so resolve the collisions.
    async def resolve_tried_collisions(self):
        async with self.lock:
            self.resolve_tried_collisions_()

    # Randomly select an address in tried that another address is attempting to evict.
    async def select_tried_collision(self):
        async with self.lock:
            return self.select_tried_collision_()

    # Choose an address to connect to.
    async def select_peer(self, new_only):
        async with self.lock:
            return self.sleect_peer_(new_only)

    # Return a bunch of addresses, selected at random.
    async def get_peers(self):
        async with self.lock:
            return self.get_peers_()

    async def mark_connected(self, addr):
        async with self.lock:
            return self.mark_connected_(addr)
