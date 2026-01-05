"""
Audio mixer - coordinates all audio sources.

Provides:
- Unified interface to all sources
- State aggregation for MQTT publishing
- Per-source control routing
- Master volume control
- Default volumes with reset-on-stop
- Volume slew rate limiting
"""

from __future__ import annotations

import logging
import subprocess
import re
import threading
import time
from typing import Callable

from media_bridge.sources import (
    AudioSource,
    SpotifySource,
    AirPlaySource,
    TVSource,
)

logger = logging.getLogger(__name__)


class AudioMixer:
    """
    Coordinates all audio sources and provides unified interface.
    
    State changes from any source are forwarded to the on_state_change callback.
    Manages master volume, per-source default volumes, and slew rate limiting.
    """

    def __init__(
        self,
        on_state_change: Callable[[str, str, any], None] | None = None,
        tv_alsa_device: str | None = None,
        pulse_sink: str | None = None,
        master_volume: int = 100,
        default_volumes: dict[str, int] | None = None,
        reset_on_stop: bool = True,
        slew_rate: int = 25,
    ):
        self._on_state_change = on_state_change
        self._running = False
        
        # Master volume (0-100) - applied to main output sink
        self._master_volume = max(0, min(100, master_volume))
        
        # Default volumes per source
        self._default_volumes = default_volumes or {
            "spotify": 15,
            "airplay": 15,
            "tv": 15,
        }
        
        # Reset to default when source stops
        self._reset_on_stop = reset_on_stop
        
        # Slew rate (%/second) - 0 means instant
        self._slew_rate = max(0, slew_rate)
        
        # Track which sources were active (for reset-on-stop detection)
        self._was_active: dict[str, bool] = {}
        
        # Slew tracking per source: {source_name: {"target": int, "current": float, "thread": Thread}}
        self._slew_state: dict[str, dict] = {}
        self._slew_lock = threading.Lock()
        
        # Initialize sources
        self._sources: dict[str, AudioSource] = {}
        
        # Spotify source
        self._sources["spotify"] = SpotifySource(
            on_state_change=self._handle_source_state_change,
            on_external_volume=self._handle_external_volume,
        )
        
        # AirPlay source
        self._sources["airplay"] = AirPlaySource(
            on_state_change=self._handle_source_state_change,
            on_external_volume=self._handle_external_volume,
        )
        
        # TV source
        self._sources["tv"] = TVSource(
            on_state_change=self._handle_source_state_change,
            on_external_volume=self._handle_external_volume,
            alsa_device=tv_alsa_device,
            pulse_sink=pulse_sink,
        )
        
        # Initialize was_active tracking
        for name in self._sources:
            self._was_active[name] = False

    def _handle_source_state_change(self, source: str, key: str, value: any) -> None:
        """Forward state changes to main callback and handle volume defaults."""
        # Check for state changes (idle/playing/paused)
        if key == "state":
            is_active = value in ("playing",)
            was_active = self._was_active.get(source, False)
            
            # Source just started playing - apply default volume
            if is_active and not was_active:
                default_vol = self._default_volumes.get(source, 15)
                logger.info(f"Source {source} started, setting volume to {default_vol}%")
                src = self._sources.get(source)
                if src:
                    # Small delay to ensure sink-input is available
                    time.sleep(0.3)
                    # Set default volume directly (slew is for user-initiated changes)
                    src.set_volume(default_vol)
                    # Notify of volume change
                    if self._on_state_change:
                        self._on_state_change(source, "volume", default_vol)
            
            # Source just became inactive - reset to default
            elif was_active and not is_active:
                # Stop any active slew first
                self.stop_slew(source)
                
                if self._reset_on_stop:
                    default_vol = self._default_volumes.get(source, 15)
                    logger.info(f"Source {source} stopped, resetting volume to {default_vol}%")
                    src = self._sources.get(source)
                    if src:
                        src.set_volume(default_vol)
                        # Notify of volume change
                        if self._on_state_change:
                            self._on_state_change(source, "volume", default_vol)
            
            self._was_active[source] = is_active
        
        # Forward to main callback
        if self._on_state_change:
            self._on_state_change(source, key, value)

    def _handle_external_volume(self, source: str, volume: int) -> None:
        """Handle external volume change from phone/app/remote.
        
        This is called when the source detects a volume change that wasn't
        initiated by us (e.g., user changed volume in Spotify app or iPhone).
        
        We DON'T call set_volume() back to the source - it already has the
        correct volume. We just notify HA of the new value immediately.
        """
        logger.info(f"External volume from {source}: {volume}%")
        
        # Stop any active slew for this source (user took over)
        self.stop_slew(source)
        
        # Just notify HA - don't call back to source (it already has the volume)
        # The source will update its own state via normal polling
        if self._on_state_change:
            self._on_state_change(source, "volume", volume)

    def start(self) -> None:
        """Start all audio sources and apply default volumes."""
        self._running = True
        
        # Apply default volumes to sources
        self.apply_default_volumes()
        
        # Apply master volume
        self.set_master_volume(self._master_volume)
        
        for name, source in self._sources.items():
            try:
                source.start()
                logger.info(f"Started source: {name}")
            except Exception as e:
                logger.error(f"Failed to start source {name}: {e}")

    def stop(self) -> None:
        """Stop all audio sources."""
        self._running = False
        
        # Stop all active slews
        with self._slew_lock:
            for source_name in list(self._slew_state.keys()):
                self._slew_state[source_name]["active"] = False
            self._slew_state.clear()
        
        for name, source in self._sources.items():
            try:
                source.stop()
                logger.info(f"Stopped source: {name}")
            except Exception as e:
                logger.error(f"Error stopping source {name}: {e}")

    def get_source(self, name: str) -> AudioSource | None:
        """Get a specific source by name."""
        return self._sources.get(name)

    def get_all_sources(self) -> dict[str, AudioSource]:
        """Get all sources."""
        return dict(self._sources)

    def get_active_sources(self) -> list[str]:
        """Get list of currently active source names."""
        return [name for name, source in self._sources.items() if source.is_active()]

    def get_all_states(self) -> dict[str, dict]:
        """Get state of all sources."""
        return {name: source.state.to_dict() for name, source in self._sources.items()}

    # Per-source controls

    def set_source_volume(self, source_name: str, volume: int, use_slew: bool = True) -> bool:
        """
        Set volume for a specific source.
        
        If slew_rate > 0 and use_slew is True, volume ramps gradually.
        """
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        
        volume = max(0, min(100, volume))
        
        # If slew disabled or rate is 0, set immediately
        if not use_slew or self._slew_rate <= 0:
            return source.set_volume(volume)
        
        # Start slewing to target
        self._start_slew(source_name, volume)
        return True
    
    def _start_slew(self, source_name: str, target: int) -> None:
        """Start ramping volume toward target at slew_rate."""
        source = self._sources.get(source_name)
        if not source:
            return
        
        with self._slew_lock:
            current_vol = source.get_volume()
            
            # If already at target, nothing to do
            if current_vol == target:
                return
            
            # Update or create slew state
            if source_name in self._slew_state:
                # Update target for existing ramp
                self._slew_state[source_name]["target"] = target
                logger.debug(f"Updated slew target for {source_name}: {target}%")
                return
            
            # Create new slew state
            self._slew_state[source_name] = {
                "target": target,
                "current": float(current_vol),
                "active": True,
            }
        
        # Start slew thread
        thread = threading.Thread(
            target=self._slew_thread,
            args=(source_name,),
            name=f"slew-{source_name}",
            daemon=True,
        )
        thread.start()
        logger.info(f"Started slew for {source_name}: {current_vol}% -> {target}% at {self._slew_rate}%/s")
    
    def _slew_thread(self, source_name: str) -> None:
        """Thread that gradually ramps volume toward target."""
        source = self._sources.get(source_name)
        if not source:
            return
        
        step_interval = 0.05  # 50ms between steps
        step_size = self._slew_rate * step_interval  # % per step
        
        try:
            while self._running:
                with self._slew_lock:
                    if source_name not in self._slew_state:
                        break
                    
                    state = self._slew_state[source_name]
                    if not state.get("active", False):
                        break
                    
                    target = state["target"]
                    current = state["current"]
                    
                    # Calculate next step
                    if current < target:
                        new_vol = min(target, current + step_size)
                    elif current > target:
                        new_vol = max(target, current - step_size)
                    else:
                        # At target
                        del self._slew_state[source_name]
                        break
                    
                    state["current"] = new_vol
                
                # Apply volume (outside lock)
                int_vol = int(round(new_vol))
                source.set_volume(int_vol)
                
                # Notify of volume change
                if self._on_state_change:
                    self._on_state_change(source_name, "volume", int_vol)
                
                # Check if we've reached target
                if abs(new_vol - target) < 0.5:
                    with self._slew_lock:
                        if source_name in self._slew_state:
                            del self._slew_state[source_name]
                    logger.debug(f"Slew complete for {source_name}: {int_vol}%")
                    break
                
                time.sleep(step_interval)
                
        except Exception as e:
            logger.error(f"Slew thread error for {source_name}: {e}")
            with self._slew_lock:
                if source_name in self._slew_state:
                    del self._slew_state[source_name]
    
    def stop_slew(self, source_name: str) -> None:
        """Stop any active slewing for a source."""
        with self._slew_lock:
            if source_name in self._slew_state:
                self._slew_state[source_name]["active"] = False

    def set_source_mute(self, source_name: str, muted: bool) -> bool:
        """Set mute for a specific source."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        return source.set_muted(muted)

    def toggle_source_mute(self, source_name: str) -> bool:
        """Toggle mute for a specific source."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        return source.toggle_mute()

    def source_play(self, source_name: str) -> bool:
        """Play a specific source."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        return source.play()

    def source_pause(self, source_name: str) -> bool:
        """Pause a specific source."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        return source.pause()

    def source_stop(self, source_name: str) -> bool:
        """Stop a specific source."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Unknown source: {source_name}")
            return False
        return source.stop_playback()

    # TV-specific settings

    def get_tv_silence_threshold(self) -> int:
        """Get TV silence detection threshold (dB)."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "get_silence_threshold"):
            return tv.get_silence_threshold()
        return -50

    def set_tv_silence_threshold(self, threshold_db: int) -> bool:
        """Set TV silence detection threshold (dB)."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "set_silence_threshold"):
            return tv.set_silence_threshold(threshold_db)
        return False

    def get_tv_silence_duration(self) -> float:
        """Get TV silence detection duration (seconds)."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "get_silence_duration"):
            return tv.get_silence_duration()
        return 3.0

    def set_tv_silence_duration(self, duration: float) -> bool:
        """Set TV silence detection duration (seconds)."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "set_silence_duration"):
            return tv.set_silence_duration(duration)
        return False

    def get_tv_level_db(self) -> float:
        """Get TV current level in dB (for debugging)."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "get_level_db"):
            return tv.get_level_db()
        return -100.0

    def set_tv_power(self, power_on: bool) -> None:
        """Set TV power state - enables/disables TV audio pipeline."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "set_tv_power"):
            tv.set_tv_power(power_on)

    def get_tv_auto_mute(self) -> bool:
        """Get TV auto-mute on silence setting."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "get_auto_mute"):
            return tv.get_auto_mute()
        return True

    def set_tv_auto_mute(self, enabled: bool) -> bool:
        """Enable/disable TV auto-mute on silence."""
        tv = self._sources.get("tv")
        if tv and hasattr(tv, "set_auto_mute"):
            return tv.set_auto_mute(enabled)
        return False

    def tv_volume_up(self, step: int = 5) -> bool:
        """Increase TV source volume by step."""
        tv = self._sources.get("tv")
        if tv:
            current = tv.get_volume()
            new_volume = min(100, current + step)
            logger.debug(f"TV volume up: {current} -> {new_volume}")
            return tv.set_volume(new_volume)
        return False

    def tv_volume_down(self, step: int = 5) -> bool:
        """Decrease TV source volume by step."""
        tv = self._sources.get("tv")
        if tv:
            current = tv.get_volume()
            new_volume = max(0, current - step)
            logger.debug(f"TV volume down: {current} -> {new_volume}")
            return tv.set_volume(new_volume)
        return False

    def tv_mute_toggle(self) -> bool:
        """Toggle TV source mute."""
        tv = self._sources.get("tv")
        if tv:
            current = tv.get_muted()
            logger.debug(f"TV mute toggle: {current} -> {not current}")
            return tv.set_muted(not current)
        return False

    # =========================================================================
    # Master Volume Control
    # =========================================================================

    def get_master_volume(self) -> int:
        """Get master volume (0-100)."""
        return self._master_volume

    def set_master_volume(self, volume: int) -> bool:
        """
        Set master volume (0-100).
        
        This controls the main output sink volume, affecting all sources.
        """
        volume = max(0, min(100, volume))
        self._master_volume = volume
        
        try:
            # Apply to default sink
            result = subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{volume}%"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info(f"Master volume set to {volume}%")
                if self._on_state_change:
                    self._on_state_change("master", "volume", volume)
                return True
            else:
                logger.error(f"Failed to set master volume: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Error setting master volume: {e}")
            return False

    def _get_current_master_volume(self) -> int:
        """Read current master volume from PulseAudio."""
        try:
            result = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                match = re.search(r'(\d+)%', result.stdout)
                if match:
                    return int(match.group(1))
        except Exception as e:
            logger.debug(f"Error reading master volume: {e}")
        return self._master_volume

    # =========================================================================
    # Default Volume Settings
    # =========================================================================

    def get_default_volume(self, source_name: str) -> int:
        """Get default volume for a source."""
        return self._default_volumes.get(source_name, 30)

    def set_default_volume(self, source_name: str, volume: int) -> bool:
        """Set default volume for a source (used when source stops)."""
        if source_name not in self._sources and source_name != "all":
            logger.warning(f"Unknown source: {source_name}")
            return False
        
        volume = max(0, min(100, volume))
        
        if source_name == "all":
            for name in self._sources:
                self._default_volumes[name] = volume
            logger.info(f"All default volumes set to {volume}%")
        else:
            self._default_volumes[source_name] = volume
            logger.info(f"Default volume for {source_name} set to {volume}%")
        
        if self._on_state_change:
            self._on_state_change(source_name, "default_volume", volume)
        
        return True

    def get_reset_on_stop(self) -> bool:
        """Get whether volumes reset when sources stop."""
        return self._reset_on_stop

    def set_reset_on_stop(self, enabled: bool) -> bool:
        """Enable/disable volume reset when sources stop."""
        self._reset_on_stop = enabled
        logger.info(f"Reset-on-stop {'enabled' if enabled else 'disabled'}")
        if self._on_state_change:
            self._on_state_change("master", "reset_on_stop", enabled)
        return True

    def apply_default_volumes(self) -> None:
        """Apply default volumes to all sources (called on startup)."""
        for name, source in self._sources.items():
            default_vol = self._default_volumes.get(name, 15)
            source.set_volume(default_vol)
            logger.info(f"Applied default volume {default_vol}% to {name}")

    # =========================================================================
    # Slew Rate Control
    # =========================================================================

    def get_slew_rate(self) -> int:
        """Get volume slew rate (%/second). 0 = instant."""
        return self._slew_rate

    def set_slew_rate(self, rate: int) -> bool:
        """Set volume slew rate (%/second). 0 = instant (no limit)."""
        self._slew_rate = max(0, rate)
        logger.info(f"Slew rate set to {self._slew_rate}%/s")
        if self._on_state_change:
            self._on_state_change("master", "slew_rate", self._slew_rate)
        return True

