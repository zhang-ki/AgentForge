import atlas.impl as atlas_core
from atlas import send as ship
from atlas.local import LocalPolicy as Policy


def deliver(item: str) -> str:
    return ship(item)
