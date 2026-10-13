"""Offline satellite reply routing and room focus. No HA client or live actions.

Only a trusted adapter may construct these inputs; this is not an LLM tool API.
Plans must be serialized, generation/volume checked, and acknowledged by that
adapter. In-memory state is deliberately not a crash-safe execution journal.
"""
from dataclasses import dataclass, field
import math
import re
import time
from typing import Literal, Mapping

from .routing import AudioFocus, RoutingError


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise RoutingError("Invalid trusted identity")


@dataclass(frozen=True)
class SatelliteRoute:
    room: str
    tts_player: str
    duck_players: tuple[str, ...]

    def __post_init__(self):
        _token(self.room)
        if not isinstance(self.duck_players, tuple):
            raise RoutingError("Duck players must be an immutable tuple")
        for entity in (self.tts_player, *self.duck_players):
            if not isinstance(entity, str) or not re.fullmatch(r"media_player\.[a-z0-9_]+", entity):
                raise RoutingError("Expected a configured media_player entity")
        if len(set(self.duck_players)) != len(self.duck_players):
            raise RoutingError("Duplicate duck target")


@dataclass(frozen=True)
class PlayerObservation:
    volume: float
    generation: str
    observed_at: float


@dataclass(frozen=True)
class SatelliteEvent:
    satellite: str
    connection: str
    sequence: int
    session: str
    kind: Literal["wake", "heartbeat", "reply_ready", "finished", "stop", "error"]
    # Opaque reference into adapter-owned, allowlisted pipeline media storage.
    # Never an arbitrary URL supplied by a model or satellite event payload.
    reply_id: str | None = None


@dataclass(frozen=True)
class VolumePlan:
    player: str
    volume: float
    expected_generation: str
    expected_volume: float
    reason: str


@dataclass(frozen=True)
class ReplyPlan:
    player: str
    reply_id: str
    session: str
    room: str
    announce: bool = True


@dataclass(frozen=True)
class AudioPlan:
    volumes: tuple[VolumePlan, ...] = ()
    reply: ReplyPlan | None = None
    deferred_players: tuple[str, ...] = ()


@dataclass
class _Session:
    satellite: str
    connection: str
    session: str
    lease: str
    players: set[str]
    expires: float
    deadline: float
    ended: bool = False
    replies: set[str] = field(default_factory=set)


class SatelliteAudio:
    """A serialized policy instance. It returns plans and never performs I/O.

    `connection` is a fresh adapter-generated epoch, not the device's claim.
    Sequence numbers are strictly increasing within it. All terminal events
    release focus; finished must mean external playback has actually completed.
    """
    def __init__(self, routes: Mapping[str, SatelliteRoute], *, factor=0.25,
                 ttl=30, max_turn=120, freshness=10, clock=time.monotonic):
        for satellite in routes:
            _token(satellite)
        if not routes or not all(isinstance(v, SatelliteRoute) for v in routes.values()):
            raise RoutingError("Explicit satellite routes required")
        if any(not math.isfinite(x) or x <= 0 for x in (ttl, max_turn, freshness)):
            raise ValueError("Invalid timing policy")
        self.routes = dict(routes)
        self.clock, self.freshness, self.max_turn = clock, freshness, max_turn
        self.focus = AudioFocus(factor=factor, ttl=ttl, clock=clock)
        self.connections: dict[str, str] = {}
        self.sequences: dict[str, int] = {}
        self.sessions: dict[tuple[str, str, str], _Session] = {}
        self.seen: dict[str, set[str]] = {}
        self._lease_counter = 0

    def _observation(self, player, observations):
        value = observations.get(player)
        if not isinstance(value, PlayerObservation):
            return None
        if (not isinstance(value.volume, (float, int)) or isinstance(value.volume, bool)
                or not math.isfinite(value.volume) or not 0 <= value.volume <= 1
                or not isinstance(value.observed_at, (float, int))
                or not math.isfinite(value.observed_at)
                or not 0 <= self.clock() - value.observed_at <= self.freshness
                or not isinstance(value.generation, str) or not value.generation):
            return None
        return value

    def connect(self, satellite, connection, observations):
        """New authenticated connection cancels old turns, preserving other leases."""
        if satellite not in self.routes:
            raise RoutingError("Unmapped satellite")
        _token(connection)
        if self.connections.get(satellite) == connection:
            return AudioPlan()
        result = self.disconnect(satellite, observations)
        self.connections[satellite] = connection
        self.sequences[satellite] = -1
        self.seen[satellite] = set()
        return result

    def disconnect(self, satellite, observations):
        if satellite not in self.routes:
            raise RoutingError("Unmapped satellite")
        self.connections.pop(satellite, None)
        for session in self.sessions.values():
            if session.satellite == satellite:
                session.ended = True
        return self.tick(observations)

    def handle(self, event: SatelliteEvent, observations):
        if event.satellite not in self.routes or self.connections.get(event.satellite) != event.connection:
            raise RoutingError("Unmapped satellite or stale connection")
        _token(event.session)
        if type(event.sequence) is not int or event.sequence < 0:
            raise RoutingError("Invalid event sequence")
        if event.kind not in {"wake", "heartbeat", "reply_ready", "finished", "stop", "error"}:
            raise RoutingError("Unsupported event")
        if event.sequence <= self.sequences[event.satellite]:
            return AudioPlan()  # Transport replay, not a lease renewal.
        key = (event.satellite, event.connection, event.session)
        session = self.sessions.get(key)
        route = self.routes[event.satellite]
        now = self.clock()
        if event.kind == "wake":
            if event.session in self.seen[event.satellite]:
                self.sequences[event.satellite] = event.sequence
                return AudioPlan()
            if len(self.seen[event.satellite]) >= 4096:
                raise RoutingError("Reconnect adapter epoch before creating more sessions")
            current = {p: self._observation(p, observations) for p in route.duck_players}
            if any(v is None for v in current.values()):
                raise RoutingError("Fresh volume/generation required for every duck target")
            self._lease_counter += 1
            session = _Session(event.satellite, event.connection, event.session,
                               str(self._lease_counter), set(route.duck_players),
                               now + self.focus.ttl, now + self.max_turn)
            self.sessions[key] = session
            self.seen[event.satellite].add(event.session)
            changes = tuple(VolumePlan(p, self.focus.acquire(p, session.lease, obs.volume, obs.generation),
                                       obs.generation, obs.volume, "wake")
                            for p, obs in sorted(current.items()))
            result = AudioPlan(changes)
        elif session is None or session.ended:
            result = AudioPlan()
        elif now >= min(session.expires, session.deadline):
            session.ended = True
            result = self.tick(observations)
        elif event.kind in {"finished", "stop", "error"}:
            session.ended = True
            result = self.tick(observations)
        elif event.kind == "heartbeat":
            # Refresh only leases whose playback generation still belongs to us.
            for player in session.players:
                state = self.focus.players.get(player)
                if state and session.lease in state.leases:
                    state.leases[session.lease] = min(now + self.focus.ttl, session.deadline)
            session.expires = min(now + self.focus.ttl, session.deadline)
            result = AudioPlan()
        else:
            _token(event.reply_id)
            if event.reply_id in session.replies:
                result = AudioPlan()
            else:
                session.replies.add(event.reply_id)
                result = AudioPlan(reply=ReplyPlan(route.tts_player, event.reply_id,
                                                  event.session, route.room))
        self.sequences[event.satellite] = event.sequence
        return result

    def tick(self, observations):
        """Expire turns and retry deferred restoration from fresh observations.

        Unlike calling AudioFocus.expire with incomplete generations, retain a
        missing-player restore until its next trusted observation. No stale write.
        """
        changes, deferred = {}, set()
        now = self.clock()
        for key, session in list(self.sessions.items()):
            if now >= min(session.expires, session.deadline):
                session.ended = True
            if not session.ended:
                continue
            for player in sorted(session.players):
                obs = self._observation(player, observations)
                if obs is None:
                    deferred.add(player)
                    continue
                volume = self.focus.release(player, session.lease, obs.generation)
                if volume is not None:
                    changes[player] = VolumePlan(player, volume, obs.generation, obs.volume, "restore")
                session.players.remove(player)
            if not session.players:
                del self.sessions[key]
        return AudioPlan(tuple(changes[p] for p in sorted(changes)),
                         deferred_players=tuple(sorted(deferred)))

    def manual_volume(self, player, desired_volume, observations):
        """Explicit human volume intent, never our own volume-command echo."""
        obs = self._observation(player, observations)
        if obs is None:
            raise RoutingError("Fresh player observation required")
        if (isinstance(desired_volume, bool) or not isinstance(desired_volume, (float, int))
                or not math.isfinite(desired_volume) or not 0 <= desired_volume <= 1):
            raise RoutingError("Invalid desired volume")
        state = self.focus.players.get(player)
        if state is None or state.generation != obs.generation:
            return AudioPlan()
        self.focus.manual_volume(player, desired_volume)
        return AudioPlan((VolumePlan(player, state.ducked_volume, obs.generation,
                                     obs.volume, "manual_intent"),))
