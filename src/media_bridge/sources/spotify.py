"""
Spotify audio source.

Uses MPRIS D-Bus interface for:
- Playback state and control
- Volume control (Spotify Connect volume)
- Metadata (title, artist)

Uses PulseAudio sink-input for:
- Level metering
- Mute control
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

try:
    import dbus
    HAS_DBUS = True
except ImportError:
    HAS_DBUS = False

from media_bridge.sources.base import AudioSource, PlaybackState, PulseAudioMixin

logger = logging.getLogger(__name__)


class SpotifySource(AudioSource, PulseAudioMixin):
    """
    Spotify audio source via spotifyd.
    
    Volume is controlled via MPRIS (affects Spotify Connect volume).
    Level metering and mute via PulseAudio sink-input.
    """

    # spotifyd uses non-standard MPRIS naming: rs.spotifyd.instance<PID>
    # But also registers standard MPRIS: org.mpris.MediaPlayer2.spotifyd.instance<PID>
    MPRIS_PREFIXES = [
        "org.mpris.MediaPlayer2.spotifyd",  # Standard MPRIS (preferred)
        "rs.spotifyd.instance",              # Non-standard fallback
    ]
    MPRIS_PATH = "/org/mpris/MediaPlayer2"
    MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
    DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

    def __init__(
        self,
        on_state_change: Callable[[str, str, any], None] | None = None,
        on_external_volume: Callable[[str, int], None] | None = None,
        poll_interval: float = 1.0,
    ):
        super().__init__(name="spotify", on_state_change=on_state_change,
                         on_external_volume=on_external_volume)
        
        self._poll_interval = poll_interval
        self._thread: threading.Thread | None = None
        self._bus: dbus.SessionBus | None = None
        self._sink_input_id: str | None = None
        self._last_level_time = 0
        self._mpris_name: str | None = None  # Discovered MPRIS bus name
        self._last_mpris_volume: int = -1  # Track last volume to detect changes

    def _init_dbus(self) -> bool:
        """Initialize D-Bus connection."""
        if not HAS_DBUS:
            logger.warning("dbus-python not available, Spotify MPRIS disabled")
            return False
        
        try:
            self._bus = dbus.SessionBus()
            return True
        except Exception as e:
            logger.error(f"Failed to connect to D-Bus: {e}")
            return False

    def _find_mpris_name(self) -> str | None:
        """Find the spotifyd MPRIS bus name (includes instance suffix)."""
        if not self._bus:
            return None
        
        try:
            # List all bus names and find spotifyd
            dbus_obj = self._bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            dbus_iface = dbus.Interface(dbus_obj, "org.freedesktop.DBus")
            names = dbus_iface.ListNames()
            
            # Check all known prefixes
            for prefix in self.MPRIS_PREFIXES:
                for name in names:
                    if name.startswith(prefix):
                        logger.debug(f"Found spotifyd MPRIS: {name}")
                        return str(name)
            
            return None
        except dbus.DBusException as e:
            logger.debug(f"Error listing D-Bus names: {e}")
            return None

    def _get_mpris_player(self):
        """Get MPRIS player proxy."""
        if not self._bus:
            return None
        
        # Find MPRIS name if not cached or stale
        if not self._mpris_name:
            self._mpris_name = self._find_mpris_name()
        
        if not self._mpris_name:
            return None
        
        try:
            obj = self._bus.get_object(self._mpris_name, self.MPRIS_PATH)
            return dbus.Interface(obj, self.MPRIS_PLAYER_IFACE)
        except dbus.DBusException:
            self._mpris_name = None  # Clear cached name, will re-discover
            return None

    def _get_mpris_props(self):
        """Get MPRIS properties interface."""
        if not self._bus:
            return None
        
        # Find MPRIS name if not cached
        if not self._mpris_name:
            self._mpris_name = self._find_mpris_name()
        
        if not self._mpris_name:
            return None
        
        try:
            obj = self._bus.get_object(self._mpris_name, self.MPRIS_PATH)
            return dbus.Interface(obj, self.DBUS_PROPS_IFACE)
        except dbus.DBusException:
            self._mpris_name = None  # Clear cached name
            return None

    def start(self) -> None:
        """Start Spotify source monitoring."""
        if not self._init_dbus():
            logger.warning("Spotify source disabled: no D-Bus")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="spotify-source")
        self._thread.start()
        logger.info("Spotify source started")

    def stop(self) -> None:
        """Stop Spotify source monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Spotify source stopped")

    def _run(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                self._poll_state()
            except Exception as e:
                logger.debug(f"Spotify poll error: {e}")
            
            time.sleep(self._poll_interval)

    def _poll_state(self) -> None:
        """Poll current state from MPRIS and PulseAudio."""
        # Always check for sink-input first (works even without MPRIS)
        self._sink_input_id = self._find_sink_input("spotifyd")
        
        # Get sink-input info for mute state and active detection
        volume = self._state.volume
        muted = False
        sink_active = False
        
        if self._sink_input_id:
            info = self._get_sink_input_info("spotifyd")
            if info:
                volume = info.get("volume", 100)  # Read volume from PulseAudio
                muted = info.get("muted", False)
                sink_active = not info.get("corked", True)
        
        # Try MPRIS for playback status, volume, and metadata
        props = self._get_mpris_props()
        state = PlaybackState.IDLE
        title = ""
        artist = ""
        
        if props:
            try:
                # Get playback status
                status = str(props.Get(self.MPRIS_PLAYER_IFACE, "PlaybackStatus"))
                
                if status == "Playing":
                    state = PlaybackState.PLAYING
                elif status == "Paused":
                    state = PlaybackState.PAUSED
                
                # Detect volume changes from Spotify app (via MPRIS)
                # When phone changes volume, we sync to PulseAudio and notify HA.
                try:
                    mpris_vol = float(props.Get(self.MPRIS_PLAYER_IFACE, "Volume"))
                    mpris_volume = int(mpris_vol * 100)
                    
                    # Detect external volume change (from Spotify app)
                    if self._last_mpris_volume >= 0 and mpris_volume != self._last_mpris_volume:
                        logger.info(f"Spotify app volume: {self._last_mpris_volume}% -> {mpris_volume}%")
                        # Sync to PulseAudio (actual audio control)
                        self._sync_volume_to_pulseaudio(mpris_volume)
                        # Use this as current volume (PA will match after sync)
                        volume = mpris_volume
                        # Notify HA of the change
                        if self.on_external_volume:
                            self.on_external_volume(self.name, mpris_volume)
                    
                    self._last_mpris_volume = mpris_volume
                except dbus.DBusException:
                    pass
                
                # Get metadata
                try:
                    metadata = props.Get(self.MPRIS_PLAYER_IFACE, "Metadata")
                    if "xesam:title" in metadata:
                        title = str(metadata["xesam:title"])
                    if "xesam:artist" in metadata:
                        artists = metadata["xesam:artist"]
                        if artists:
                            artist = str(artists[0]) if len(artists) == 1 else ", ".join(str(a) for a in artists)
                except dbus.DBusException:
                    pass
                    
            except dbus.DBusException as e:
                logger.debug(f"MPRIS error: {e}")
        
        # Fall back to sink-input state if MPRIS not available
        if state == PlaybackState.IDLE and sink_active:
            state = PlaybackState.PLAYING
        
        self._update_state(
            state=state,
            volume=volume,
            muted=muted,
            title=title,
            artist=artist,
        )

    def is_active(self) -> bool:
        """Check if Spotify is playing."""
        return self._state.state == PlaybackState.PLAYING

    def get_volume(self) -> int:
        """Get Spotify Connect volume."""
        return self._state.volume

    def set_volume(self, volume: int) -> bool:
        """Set Spotify volume via PulseAudio AND sync to MPRIS.
        
        PulseAudio is the source of truth (actual audio level).
        MPRIS is updated so the Spotify app shows the correct volume.
        spotifyd has --volume-controller none, so MPRIS doesn't affect audio.
        """
        volume = max(0, min(100, volume))
        
        # Set PulseAudio sink-input volume (the actual audio control)
        if not self._sink_input_id:
            self._sink_input_id = self._find_sink_input("spotifyd")
        
        pa_success = False
        if self._sink_input_id:
            pa_success = self._set_sink_input_volume(self._sink_input_id, volume)
        
        # Also update MPRIS so Spotify app shows correct value
        props = self._get_mpris_props()
        if props:
            try:
                props.Set(
                    self.MPRIS_PLAYER_IFACE,
                    "Volume",
                    dbus.Double(volume / 100.0),
                )
                # Track this to avoid detecting it as an external change
                self._last_mpris_volume = volume
            except dbus.DBusException as e:
                logger.debug(f"MPRIS volume sync failed: {e}")
        
        if pa_success:
            self._update_state(volume=volume)
        else:
            logger.warning("No Spotify sink-input found for volume")
        
        return pa_success
    
    def _sync_volume_to_pulseaudio(self, volume: int) -> None:
        """Sync external volume change to PulseAudio sink-input."""
        if not self._sink_input_id:
            self._sink_input_id = self._find_sink_input("spotifyd")
        
        if self._sink_input_id:
            self._set_sink_input_volume(self._sink_input_id, volume)
            logger.debug(f"Synced Spotify volume {volume}% to PulseAudio")

    def get_muted(self) -> bool:
        """Get mute state from sink-input."""
        return self._state.muted

    def set_muted(self, muted: bool) -> bool:
        """Set mute via sink-input."""
        # Always refresh sink-input ID (it can change on spotifyd restart)
        self._sink_input_id = self._find_sink_input("spotifyd")
        
        if not self._sink_input_id:
            logger.warning("No Spotify sink-input found for mute")
            return False
        
        logger.info(f"Setting Spotify mute={muted} on sink-input {self._sink_input_id}")
        success = self._set_sink_input_mute(self._sink_input_id, muted)
        if success:
            self._update_state(muted=muted)
            logger.info(f"Spotify mute set to {muted}")
        else:
            logger.warning(f"Failed to set Spotify mute on sink-input {self._sink_input_id}")
        return success

    def get_level(self) -> int:
        """Get current audio level (stub - needs peak detection)."""
        # TODO: Implement proper level metering via PulseAudio
        # For now, return 50 if playing, 0 otherwise
        if self._state.state == PlaybackState.PLAYING and not self._state.muted:
            return 50
        return 0

    def play(self) -> bool:
        """Resume Spotify playback."""
        player = self._get_mpris_player()
        if not player:
            return False
        
        try:
            player.Play()
            return True
        except dbus.DBusException as e:
            logger.error(f"Failed to play: {e}")
            return False

    def pause(self) -> bool:
        """Pause Spotify playback."""
        player = self._get_mpris_player()
        if not player:
            return False
        
        try:
            player.Pause()
            return True
        except dbus.DBusException as e:
            logger.error(f"Failed to pause: {e}")
            return False

    def stop_playback(self) -> bool:
        """Stop Spotify playback."""
        player = self._get_mpris_player()
        if not player:
            return False
        
        try:
            player.Stop()
            return True
        except dbus.DBusException as e:
            logger.error(f"Failed to stop: {e}")
            return False

