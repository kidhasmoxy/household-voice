"""Pure routing, presence, and audio-focus policies. Does not operate speakers."""
from dataclasses import dataclass, field
import time


class RoutingError(ValueError):
    pass


class RoomRegistry:
    def __init__(self, rooms, announcement_defaults=None, announcement_groups=None):
        self.rooms = rooms
        self.announcement_defaults = announcement_defaults
        self.announcement_groups = announcement_groups or {}
        self.aliases = {}
        self.satellites = {}
        for area, config in rooms.items():
            for alias in [area, *config.get("aliases", [])]:
                key = alias.casefold().strip()
                if key in self.aliases and self.aliases[key] != area:
                    raise RoutingError("Ambiguous room alias")
                self.aliases[key] = area
            for satellite in config.get("satellites", []):
                if satellite in self.satellites:
                    raise RoutingError("Satellite assigned to multiple rooms")
                self.satellites[satellite] = area
        for names in ([announcement_defaults] if announcement_defaults is not None else []) + list(self.announcement_groups.values()):
            self._announcement_rooms(names)

    def resolve(self, source_device=None, target=None):
        area = self.aliases.get(target.casefold().strip()) if target else self.satellites.get(source_device)
        if area is None:
            raise RoutingError("Unknown target or source; ask which room")
        config = self.rooms[area]
        if not config.get("music_player") or not config.get("announcement_player"):
            raise RoutingError("Room mapping is incomplete")
        return {"area": area, **config}

    def broadcast_targets(self):
        return sorted({c["announcement_player"] for c in self.rooms.values()
                       if c.get("broadcast_enabled", True) and c.get("announcement_player")})

    def _announcement_rooms(self, names):
        if not isinstance(names, list) or not names or any(not isinstance(name,str) for name in names):
            raise RoutingError('Announcement rooms must be a nonempty list')
        result=set()
        for name in names:
            area=self.aliases.get(name.casefold().strip())
            if area is None or not self.rooms[area].get('announcement_player'):
                raise RoutingError('Unknown announcement room; no speakers selected')
            result.add(self.rooms[area]['announcement_player'])
        return sorted(result)

    def announcement_targets(self, rooms=None, group=None):
        """No target means configured default; an invalid explicit target fails."""
        if rooms is not None and group is not None:
            raise RoutingError('Choose rooms or a named group')
        if rooms is not None:
            return self._announcement_rooms(rooms)
        if group is not None:
            if group not in self.announcement_groups:
                raise RoutingError('Unknown announcement group')
            return self._announcement_rooms(self.announcement_groups[group])
        return (self._announcement_rooms(self.announcement_defaults)
                if self.announcement_defaults is not None else self.broadcast_targets())


def home_phones(phones, now, freshness=300):
    """Phone-specific observation, not person aggregate. Unknown/future timestamps fail closed."""
    return sorted({p["notify_target"] for p in phones
                   if p.get("state") == "home" and p.get("notify_target")
                   and isinstance(p.get("observed_at"), (float, int))
                   and 0 <= now - p["observed_at"] <= freshness})


@dataclass
class FocusState:
    restore_volume: float
    ducked_volume: float
    generation: str
    leases: dict = field(default_factory=dict)


class AudioFocus:
    """Returns desired volume changes; the reviewed HA adapter must execute them."""
    def __init__(self, factor=0.25, ttl=30, clock=time.monotonic):
        if not 0 < factor <= 1 or ttl <= 0:
            raise ValueError("Invalid focus settings")
        self.factor, self.ttl, self.clock = factor, ttl, clock
        self.players = {}

    def acquire(self, player, session, current_volume, generation):
        if not 0 <= current_volume <= 1:
            raise ValueError("Volume outside 0..1")
        state = self.players.get(player)
        if state and state.generation != generation:
            self.players.pop(player)
            state = None
        if state is None:
            state = FocusState(current_volume, current_volume * self.factor, generation)
            self.players[player] = state
        state.leases[session] = self.clock() + self.ttl
        return state.ducked_volume

    def manual_volume(self, player, desired_volume):
        if not 0 <= desired_volume <= 1:
            raise ValueError("Volume outside 0..1")
        state = self.players.get(player)
        if state:
            state.restore_volume = desired_volume
            state.ducked_volume = desired_volume * self.factor

    def release(self, player, session, generation):
        state = self.players.get(player)
        if state is None:
            return None
        state.leases.pop(session, None)
        return self._restore_if_idle(player, state, generation)

    def _restore_if_idle(self, player, state, generation):
        state.leases = {k: v for k, v in state.leases.items() if v > self.clock()}
        if state.leases:
            return None
        self.players.pop(player, None)
        return state.restore_volume if state.generation == generation else None

    def expire(self, current_generations):
        changes = {}
        for player, state in list(self.players.items()):
            result = self._restore_if_idle(player, state, current_generations.get(player))
            if result is not None:
                changes[player] = result
        return changes

