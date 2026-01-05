"""
Base class for audio sources.

Each source (Spotify, AirPlay, TV) implements this interface for
unified volume, mute, level, and state management.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class PlaybackState(Enum):
    """Playback states."""
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class SourceState:
    """Current state of an audio source."""
    name: str
    state: PlaybackState = PlaybackState.IDLE
    volume: int = 100  # 0-100
    muted: bool = False
    level: int = 0  # 0-100, current audio level (VU)
    level_db: float = -100.0  # Current level in dB
    title: str = ""
    artist: str = ""
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "volume": self.volume,
            "muted": self.muted,
            "level": self.level,
            "level_db": self.level_db,
            "title": self.title,
            "artist": self.artist,
        }


class StateCallback:
    """Protocol for state change callbacks."""
    def __call__(self, source: str, key: str, value: any) -> None: ...


class AudioSource(ABC):
    """
    Abstract base class for audio sources.
    
    Each source implementation must provide:
    - Detection of when the source is active
    - Volume control (get/set)
    - Mute control (get/set)
    - Level metering (current audio level)
    - Metadata (title, artist) where available
    """

    def __init__(
        self,
        name: str,
        on_state_change: Callable[[str, str, any], None] | None = None,
        on_external_volume: Callable[[str, int], None] | None = None,
    ):
        self.name = name
        self.on_state_change = on_state_change
        # Callback for external volume changes (from phone, app, remote)
        # This allows the mixer to apply slew rate and other processing
        self.on_external_volume = on_external_volume
        
        self._state = SourceState(name=name)
        self._lock = threading.RLock()  # Reentrant lock - callbacks may re-acquire
        self._running = False

    @property
    def state(self) -> SourceState:
        """Get current source state (thread-safe copy)."""
        with self._lock:
            return SourceState(
                name=self._state.name,
                state=self._state.state,
                volume=self._state.volume,
                muted=self._state.muted,
                level=self._state.level,
                title=self._state.title,
                artist=self._state.artist,
                last_update=self._state.last_update,
            )

    def _notify(self, key: str, value: any) -> None:
        """Notify callback of state change."""
        if self.on_state_change:
            self.on_state_change(self.name, key, value)

    def _update_state(self, **kwargs) -> None:
        """Update state and notify on changes."""
        with self._lock:
            changed = False
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    current = getattr(self._state, key)
                    if current != value:
                        setattr(self._state, key, value)
                        changed = True
                        self._notify(key, value.value if isinstance(value, Enum) else value)
            
            if changed:
                self._state.last_update = time.time()

    @abstractmethod
    def start(self) -> None:
        """Start the source (detection, monitoring)."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the source."""
        pass

    @abstractmethod
    def is_active(self) -> bool:
        """Check if this source is currently active/playing."""
        pass

    @abstractmethod
    def get_volume(self) -> int:
        """Get current volume (0-100)."""
        pass

    @abstractmethod
    def set_volume(self, volume: int) -> bool:
        """Set volume (0-100). Returns success."""
        pass

    @abstractmethod
    def get_muted(self) -> bool:
        """Get current mute state."""
        pass

    @abstractmethod
    def set_muted(self, muted: bool) -> bool:
        """Set mute state. Returns success."""
        pass

    def toggle_mute(self) -> bool:
        """Toggle mute state."""
        return self.set_muted(not self.get_muted())

    @abstractmethod
    def get_level(self) -> int:
        """Get current audio level (0-100, for VU meter)."""
        pass

    # Playback control (optional, not all sources support this)
    
    def play(self) -> bool:
        """Resume playback. Returns success."""
        return False

    def pause(self) -> bool:
        """Pause playback. Returns success."""
        return False

    def stop_playback(self) -> bool:
        """Stop playback. Returns success."""
        return False


class PulseAudioMixin:
    """
    Mixin providing PulseAudio sink-input operations.
    
    Used by sources that output to PulseAudio (Spotify, AirPlay, TV).
    """

    def _find_sink_input(self, pattern: str) -> str | None:
        """Find sink-input ID matching pattern in application.process.binary."""
        try:
            result = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            
            current_id = None
            for line in result.stdout.split('\n'):
                stripped = line.strip()
                if stripped.startswith("Sink Input #"):
                    current_id = stripped.split("#")[1]
                elif "application.process.binary" in stripped and pattern in stripped.lower():
                    return current_id
            
            return None
        except Exception as e:
            logger.debug(f"Error finding sink-input: {e}")
            return None

    def _get_sink_input_volume(self, sink_input_id: str) -> int | None:
        """Get volume of a sink-input (0-100)."""
        try:
            result = subprocess.run(
                ["pactl", "get-sink-input-volume", sink_input_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                match = re.search(r'(\d+)%', result.stdout)
                if match:
                    return int(match.group(1))
        except Exception as e:
            logger.debug(f"Error getting sink-input volume: {e}")
        return None

    def _set_sink_input_volume(self, sink_input_id: str, volume: int) -> bool:
        """Set volume of a sink-input (0-100)."""
        try:
            volume = max(0, min(100, volume))
            result = subprocess.run(
                ["pactl", "set-sink-input-volume", sink_input_id, f"{volume}%"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error setting sink-input volume: {e}")
            return False

    def _get_sink_input_mute(self, sink_input_id: str) -> bool | None:
        """Get mute state of a sink-input."""
        try:
            result = subprocess.run(
                ["pactl", "get-sink-input-mute", sink_input_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return "yes" in result.stdout.lower()
        except Exception as e:
            logger.debug(f"Error getting sink-input mute: {e}")
        return None

    def _set_sink_input_mute(self, sink_input_id: str, muted: bool) -> bool:
        """Set mute state of a sink-input."""
        try:
            result = subprocess.run(
                ["pactl", "set-sink-input-mute", sink_input_id, "1" if muted else "0"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error setting sink-input mute: {e}")
            return False

    def _get_sink_input_info(self, pattern: str) -> dict | None:
        """Get full info about a sink-input matching pattern."""
        try:
            result = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            
            current = {}
            found = False
            
            for line in result.stdout.split('\n'):
                stripped = line.strip()
                
                if stripped.startswith("Sink Input #"):
                    if found and current:
                        return current
                    current = {"id": stripped.split("#")[1]}
                    found = False
                elif current:
                    if ": " in stripped and "=" not in stripped:
                        key, _, value = stripped.partition(": ")
                        key = key.strip()
                        value = value.strip()
                        
                        if key == "Corked":
                            current["corked"] = value.lower() == "yes"
                        elif key == "Mute":
                            current["muted"] = value.lower() == "yes"
                        elif key == "Volume":
                            match = re.search(r'(\d+)%', value)
                            if match:
                                current["volume"] = int(match.group(1))
                    
                    elif " = " in stripped:
                        key, _, value = stripped.partition(" = ")
                        key = key.strip()
                        value = value.strip().strip('"')
                        
                        if key == "application.process.binary":
                            current["binary"] = value
                            if pattern in value.lower():
                                found = True
            
            if found and current:
                return current
            
            return None
        except Exception as e:
            logger.debug(f"Error getting sink-input info: {e}")
            return None

