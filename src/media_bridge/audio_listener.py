"""
Audio stack listener for detecting playback sources.

Uses PulseAudio sink-inputs for reliable source detection and D-Bus MPRIS for metadata.
Supports multiple simultaneous sources (Spotify, AirPlay, etc.)
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class PlaybackState(Enum):
    """Playback states."""
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class AudioSource(Enum):
    """Known audio sources."""
    SPOTIFY = "spotify"
    AIRPLAY = "airplay"
    TV = "tv"
    UNKNOWN = "unknown"


@dataclass
class SourceState:
    """State of a single audio source."""
    state: PlaybackState = PlaybackState.IDLE
    volume: int = 100  # Source-specific volume (0-100)
    title: str = ""
    artist: str = ""
    album: str = ""


@dataclass
class AudioState:
    """Current audio/playback state with multiple sources."""
    sources: dict[str, SourceState] = field(default_factory=dict)
    active_sources: list[str] = field(default_factory=list)
    primary_source: str = "unknown"
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Convert to dict for MQTT publishing."""
        return {
            "playback_state": self._get_primary_state().value,
            "playback_source": self.primary_source,
            "active_sources": self.active_sources,
            "title": self._get_metadata("title"),
            "artist": self._get_metadata("artist"),
        }

    def _get_primary_state(self) -> PlaybackState:
        """Get state of primary/active source."""
        if self.primary_source in self.sources:
            return self.sources[self.primary_source].state
        if self.active_sources:
            first = self.active_sources[0]
            if first in self.sources:
                return self.sources[first].state
        return PlaybackState.IDLE

    def _get_metadata(self, field: str) -> str:
        """Get metadata from primary source."""
        if self.primary_source in self.sources:
            return getattr(self.sources[self.primary_source], field, "")
        return ""


# Map application names/binaries to source types
SOURCE_PATTERNS = {
    "spotify": AudioSource.SPOTIFY,
    "spotifyd": AudioSource.SPOTIFY,
    "shairplay": AudioSource.AIRPLAY,
    "shairport": AudioSource.AIRPLAY,
    "airplay": AudioSource.AIRPLAY,
}


class AudioListener:
    """
    Listens to audio stack for playback state.
    
    Detects sources via:
    - PulseAudio sink-inputs (reliable, detects all audio)
    - D-Bus MPRIS (for metadata from Spotify, etc.)
    """

    def __init__(
        self,
        on_state_change: Callable[[str, str | list], None] | None = None,
        poll_interval: float = 1.0,
    ):
        self.on_state_change = on_state_change
        self.poll_interval = poll_interval
        
        self._state = AudioState()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        
        # D-Bus for MPRIS metadata
        self._dbus_available = False
        self._bus = None

    @property
    def state(self) -> AudioState:
        with self._lock:
            return AudioState(
                sources=self._state.sources.copy(),
                active_sources=self._state.active_sources.copy(),
                primary_source=self._state.primary_source,
                last_update=self._state.last_update,
            )

    def _init_dbus(self) -> bool:
        """Initialize D-Bus connection for MPRIS."""
        try:
            import dbus
            self._bus = dbus.SessionBus()
            self._dbus_available = True
            logger.info("D-Bus initialized for MPRIS monitoring")
            return True
        except ImportError:
            logger.warning("python-dbus not installed, MPRIS metadata disabled")
            return False
        except Exception as e:
            logger.warning(f"Failed to connect to D-Bus: {e}")
            return False

    def _get_pulse_sink_inputs(self) -> list[dict]:
        """Get list of active sink inputs from PulseAudio."""
        try:
            result = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return []
            
            inputs = []
            current = {}
            
            for line in result.stdout.split('\n'):
                stripped = line.strip()
                
                if stripped.startswith("Sink Input #"):
                    if current:
                        inputs.append(current)
                    current = {"id": stripped.split("#")[1]}
                elif current:
                    # Handle "Key: Value" format
                    if ": " in stripped and "=" not in stripped:
                        key, _, value = stripped.partition(": ")
                        key = key.strip()
                        value = value.strip().strip('"')
                        
                        if key == "Corked":
                            current["corked"] = value.lower() == "yes"
                        elif key == "Volume":
                            # Parse volume percentage from "front-left: 65536 / 100% / 0.00 dB, ..."
                            match = re.search(r'(\d+)%', value)
                            if match:
                                current["volume"] = int(match.group(1))
                    
                    # Handle "key = value" format (properties)
                    elif " = " in stripped:
                        key, _, value = stripped.partition(" = ")
                        key = key.strip()
                        value = value.strip().strip('"')
                        
                        if key == "application.name":
                            current["app_name"] = value
                        elif key == "application.process.binary":
                            current["binary"] = value
                        elif key == "media.name":
                            current["media_name"] = value
            
            if current:
                inputs.append(current)
            
            return inputs
            
        except Exception as e:
            logger.debug(f"Failed to get sink inputs: {e}")
            return []

    def _detect_source_type(self, sink_input: dict) -> AudioSource:
        """Detect source type from sink input properties."""
        # Check binary name
        binary = sink_input.get("binary", "").lower()
        for pattern, source in SOURCE_PATTERNS.items():
            if pattern in binary:
                return source
        
        # Check application name
        app_name = sink_input.get("app_name", "").lower()
        for pattern, source in SOURCE_PATTERNS.items():
            if pattern in app_name:
                return source
        
        return AudioSource.UNKNOWN

    def _get_mpris_metadata(self, source: AudioSource) -> dict:
        """Get metadata and volume from MPRIS D-Bus interface."""
        if not self._dbus_available or not self._bus:
            return {}
        
        try:
            import dbus
            
            # Find MPRIS player
            obj = self._bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            iface = dbus.Interface(obj, "org.freedesktop.DBus")
            names = iface.ListNames()
            
            # Look for matching player
            target = None
            for name in names:
                if not name.startswith("org.mpris.MediaPlayer2."):
                    continue
                name_lower = name.lower()
                if source == AudioSource.SPOTIFY and ("spotify" in name_lower or "spotifyd" in name_lower):
                    target = name
                    break
            
            if not target:
                return {}
            
            obj = self._bus.get_object(target, "/org/mpris/MediaPlayer2")
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            
            metadata = {}
            
            # Get track metadata
            try:
                meta = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
                if "xesam:title" in meta:
                    metadata["title"] = str(meta["xesam:title"])
                if "xesam:artist" in meta:
                    artists = meta["xesam:artist"]
                    if artists:
                        metadata["artist"] = str(artists[0]) if hasattr(artists, "__iter__") else str(artists)
                if "xesam:album" in meta:
                    metadata["album"] = str(meta["xesam:album"])
            except Exception:
                pass
            
            # Get volume (0.0 - 1.0 range)
            try:
                volume = props.Get("org.mpris.MediaPlayer2.Player", "Volume")
                metadata["volume"] = int(float(volume) * 100)
            except Exception:
                pass
            
            return metadata
            
        except Exception as e:
            logger.debug(f"Error getting MPRIS metadata: {e}")
            return {}

    def _update_state(
        self,
        active_sources: list[str],
        source_states: dict[str, SourceState],
    ) -> None:
        """Update internal state and notify callbacks."""
        with self._lock:
            changed = False
            
            # Determine primary source (prefer spotify, then airplay, then first active)
            old_primary = self._state.primary_source
            if "spotify" in active_sources:
                new_primary = "spotify"
            elif "airplay" in active_sources:
                new_primary = "airplay"
            elif active_sources:
                new_primary = active_sources[0]
            else:
                new_primary = "unknown"
            
            # Check for changes
            if set(active_sources) != set(self._state.active_sources):
                self._state.active_sources = active_sources
                changed = True
                if self.on_state_change:
                    self.on_state_change("active_sources", active_sources)
            
            if new_primary != old_primary:
                self._state.primary_source = new_primary
                changed = True
                if self.on_state_change:
                    self.on_state_change("playback_source", new_primary)
            
            # Update source states and check for per-source changes
            for source_name, source_state in source_states.items():
                old_state = self._state.sources.get(source_name)
                state_changed = old_state is None or old_state.state != source_state.state
                volume_changed = old_state is None or old_state.volume != source_state.volume
                
                if state_changed or volume_changed:
                    changed = True
                    
                    # Publish per-source state
                    if self.on_state_change:
                        self.on_state_change(f"source/{source_name}/state", source_state.state.value)
                        self.on_state_change(f"source/{source_name}/volume", source_state.volume)
                
                self._state.sources[source_name] = source_state
            
            # Clean up inactive sources
            for source_name in list(self._state.sources.keys()):
                if source_name not in source_states:
                    # Publish idle state for removed source
                    if self.on_state_change:
                        self.on_state_change(f"source/{source_name}/state", PlaybackState.IDLE.value)
                    del self._state.sources[source_name]
            
            # Publish overall playback state based on primary source
            if changed:
                self._state.last_update = time.time()
                
                if new_primary in source_states:
                    state = source_states[new_primary].state
                    if self.on_state_change:
                        self.on_state_change("playback_state", state.value)
                    
                    # Also publish metadata if available
                    title = source_states[new_primary].title
                    artist = source_states[new_primary].artist
                    if title and self.on_state_change:
                        self.on_state_change("title", title)
                    if artist and self.on_state_change:
                        self.on_state_change("artist", artist)
                elif not active_sources:
                    if self.on_state_change:
                        self.on_state_change("playback_state", PlaybackState.IDLE.value)
                
                logger.info(f"Audio: active={active_sources}, primary={new_primary}")

    def _poll_state(self) -> None:
        """Poll all audio sources for state."""
        # Get active sink inputs from PulseAudio
        sink_inputs = self._get_pulse_sink_inputs()
        
        active_sources = []
        source_states = {}
        
        for sink_input in sink_inputs:
            source_type = self._detect_source_type(sink_input)
            source_name = source_type.value
            
            if source_name == "unknown":
                continue
            
            # Determine if playing or paused
            is_corked = sink_input.get("corked", False)
            state = PlaybackState.PAUSED if is_corked else PlaybackState.PLAYING
            
            # Get stream volume from sink-input (fallback)
            stream_volume = sink_input.get("volume", 100)
            
            # Get metadata from MPRIS if available (includes volume for Spotify)
            metadata = {}
            if source_type == AudioSource.SPOTIFY:
                metadata = self._get_mpris_metadata(source_type)
            
            # Prefer MPRIS volume over stream volume for Spotify
            volume = metadata.get("volume", stream_volume)
            
            source_states[source_name] = SourceState(
                state=state,
                volume=volume,
                title=metadata.get("title", ""),
                artist=metadata.get("artist", ""),
                album=metadata.get("album", ""),
            )
            
            if state == PlaybackState.PLAYING:
                active_sources.append(source_name)
        
        self._update_state(active_sources, source_states)

    def _run(self) -> None:
        """Main listener loop."""
        logger.info("Audio listener thread started")
        
        self._init_dbus()
        
        while self._running:
            try:
                self._poll_state()
                time.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"Error in audio listener loop: {e}")
                time.sleep(2.0)
        
        logger.info("Audio listener thread stopped")

    def start(self) -> None:
        """Start the audio listener thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, name="audio-listener", daemon=True)
        self._thread.start()
        logger.info("Audio listener started")

    def stop(self) -> None:
        """Stop the audio listener thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Audio listener stopped")

    # Playback control methods

    def play(self) -> bool:
        """Send play command to active source."""
        return self._send_mpris_command("Play")

    def pause(self) -> bool:
        """Send pause command to active source."""
        return self._send_mpris_command("Pause")

    def stop_playback(self) -> bool:
        """Send stop command to active source."""
        return self._send_mpris_command("Stop")

    def _send_mpris_command(self, command: str) -> bool:
        """Send a command via MPRIS D-Bus interface."""
        if not self._dbus_available or not self._bus:
            logger.warning("D-Bus not available")
            return False
        
        try:
            import dbus
            
            obj = self._bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            iface = dbus.Interface(obj, "org.freedesktop.DBus")
            names = iface.ListNames()
            
            # Find MPRIS players
            players = [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]
            if not players:
                logger.warning("No MPRIS players found")
                return False
            
            # Prefer spotifyd
            target = players[0]
            for p in players:
                if "spotify" in p.lower():
                    target = p
                    break
            
            obj = self._bus.get_object(target, "/org/mpris/MediaPlayer2")
            player = dbus.Interface(obj, "org.mpris.MediaPlayer2.Player")
            
            getattr(player, command)()
            logger.info(f"Sent {command} to {target}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending MPRIS command: {e}")
            return False

    def set_source_volume(self, source: str, volume: int) -> bool:
        """Set volume for a specific source (0-100)."""
        volume = max(0, min(100, volume))
        
        if source == "spotify":
            return self._set_spotify_volume(volume)
        elif source == "airplay":
            # AirPlay volume is controlled by the sender, can't set it here
            logger.warning("AirPlay volume is controlled by the sending device")
            return False
        else:
            logger.warning(f"Unknown source: {source}")
            return False

    def _set_spotify_volume(self, volume: int) -> bool:
        """Set Spotify volume via MPRIS (0-100)."""
        if not self._dbus_available or not self._bus:
            logger.warning("D-Bus not available")
            return False
        
        try:
            import dbus
            
            obj = self._bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            iface = dbus.Interface(obj, "org.freedesktop.DBus")
            names = iface.ListNames()
            
            # Find spotifyd
            target = None
            for name in names:
                if name.startswith("org.mpris.MediaPlayer2.") and "spotify" in name.lower():
                    target = name
                    break
            
            if not target:
                logger.warning("Spotifyd not found on D-Bus")
                return False
            
            obj = self._bus.get_object(target, "/org/mpris/MediaPlayer2")
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            
            # Set volume (MPRIS uses 0.0-1.0 range)
            props.Set(
                "org.mpris.MediaPlayer2.Player",
                "Volume",
                dbus.Double(volume / 100.0)
            )
            
            logger.info(f"Set Spotify volume to {volume}%")
            return True
            
        except Exception as e:
            logger.error(f"Error setting Spotify volume: {e}")
            return False
