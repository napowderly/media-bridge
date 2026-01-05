"""
AirPlay audio source via shairport-sync.

Uses:
- Metadata pipe for volume and playback state from AirPlay protocol
- PulseAudio sink-input for local volume/mute control

The metadata pipe provides:
- Volume changes from the phone (pvol)
- Playback start/end events
- Track metadata (optional)

Metadata format (XML):
<item><type>73736e63</type><code>70766f6c</code><length>N</length>
<data encoding="base64">BASE64DATA==</data></item>

Where type/code are hex-encoded ASCII (e.g., 73736e63 = "ssnc", 70766f6c = "pvol")
"""

from __future__ import annotations

import base64
import logging
import os
import re
import select
import threading
import time
from typing import Callable

from media_bridge.sources.base import AudioSource, PlaybackState, PulseAudioMixin

logger = logging.getLogger(__name__)

# Regex to parse shairport-sync XML metadata items
METADATA_ITEM_RE = re.compile(
    r'<item><type>([0-9a-fA-F]+)</type><code>([0-9a-fA-F]+)</code>'
    r'<length>(\d+)</length>\s*'
    r'(?:<data encoding="base64">\s*([^<]*)</data>)?</item>',
    re.DOTALL
)


class AirPlaySource(AudioSource, PulseAudioMixin):
    """
    AirPlay audio source via shairport-sync.
    
    Reads volume from metadata pipe, controls via PulseAudio sink-input.
    """

    METADATA_PIPE = "/tmp/shairport-sync-metadata"
    SINK_INPUT_PATTERNS = ["shairport-sync", "shairport"]

    def __init__(
        self,
        on_state_change: Callable[[str, str, any], None] | None = None,
        on_external_volume: Callable[[str, int], None] | None = None,
        poll_interval: float = 1.0,
        metadata_pipe: str | None = None,
    ):
        super().__init__(name="airplay", on_state_change=on_state_change, 
                         on_external_volume=on_external_volume)
        
        self._poll_interval = poll_interval
        self._metadata_pipe = metadata_pipe or self.METADATA_PIPE
        self._thread: threading.Thread | None = None
        self._metadata_thread: threading.Thread | None = None
        self._sink_input_id: str | None = None
        
        # AirPlay volume from phone (-144 to 0, where -144 is mute)
        self._airplay_volume: float = 0.0
        self._airplay_muted: bool = False

    def start(self) -> None:
        """Start AirPlay source monitoring."""
        self._running = True
        
        # Start metadata pipe reader thread
        self._metadata_thread = threading.Thread(
            target=self._run_metadata_reader,
            daemon=True,
            name="airplay-metadata",
        )
        self._metadata_thread.start()
        
        # Start state polling thread
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="airplay-source",
        )
        self._thread.start()
        
        logger.info("AirPlay source started")

    def stop(self) -> None:
        """Stop AirPlay source monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._metadata_thread:
            self._metadata_thread.join(timeout=5)
            self._metadata_thread = None
        logger.info("AirPlay source stopped")

    def _run_metadata_reader(self) -> None:
        """Read metadata from shairport-sync pipe (XML format)."""
        logger.info(f"Starting metadata reader for {self._metadata_pipe}")
        
        buffer = ""
        
        while self._running:
            try:
                # Check if pipe exists
                if not os.path.exists(self._metadata_pipe):
                    time.sleep(1)
                    continue
                
                # Open pipe (non-blocking)
                fd = os.open(self._metadata_pipe, os.O_RDONLY | os.O_NONBLOCK)
                pipe = os.fdopen(fd, 'r', encoding='utf-8', errors='replace')
                
                logger.info("Metadata pipe opened")
                buffer = ""
                
                while self._running:
                    # Use select to avoid blocking forever
                    readable, _, _ = select.select([pipe], [], [], 1.0)
                    if not readable:
                        continue
                    
                    # Read available data
                    chunk = pipe.read(4096)
                    if not chunk:
                        # Pipe closed
                        break
                    
                    buffer += chunk
                    
                    # Extract complete <item>...</item> elements
                    while True:
                        match = METADATA_ITEM_RE.search(buffer)
                        if not match:
                            # No complete item, trim buffer if it's getting too long
                            if len(buffer) > 10000:
                                # Find last complete item end
                                last_end = buffer.rfind('</item>')
                                if last_end > 0:
                                    buffer = buffer[last_end + 7:]
                            break
                        
                        # Process the item
                        type_hex = match.group(1)
                        code_hex = match.group(2)
                        data_b64 = match.group(4) or ""
                        
                        self._process_metadata_xml(type_hex, code_hex, data_b64)
                        
                        # Remove processed item from buffer
                        buffer = buffer[match.end():]
                
                pipe.close()
                
            except OSError as e:
                if e.errno == 6:  # No such device or address (pipe closed)
                    logger.debug("Metadata pipe closed, will retry")
                else:
                    logger.debug(f"Metadata pipe error: {e}")
                time.sleep(1)
            except Exception as e:
                logger.debug(f"Metadata reader error: {e}")
                time.sleep(1)
        
        logger.info("Metadata reader stopped")

    def _hex_to_ascii(self, hex_str: str) -> str:
        """Convert hex string to ASCII (e.g., '73736e63' -> 'ssnc')."""
        try:
            return bytes.fromhex(hex_str).decode('ascii')
        except (ValueError, UnicodeDecodeError):
            return hex_str

    def _process_metadata_xml(self, type_hex: str, code_hex: str, data_b64: str) -> None:
        """Process a metadata item from the XML pipe format."""
        try:
            type_str = self._hex_to_ascii(type_hex)
            code_str = self._hex_to_ascii(code_hex)
            
            # Decode base64 data if present
            data = base64.b64decode(data_b64) if data_b64.strip() else b''
            
            logger.debug(f"Metadata: type={type_str} code={code_str} data_len={len(data)}")
            
            # Volume: ssnc pvol
            if type_str == 'ssnc' and code_str == 'pvol':
                # Volume data format: "airplay_volume,volume,lowest_volume,highest_volume"
                # airplay_volume is -144 (mute) to 0 (max)
                try:
                    parts = data.decode('ascii').split(',')
                    if len(parts) >= 1:
                        airplay_vol = float(parts[0])
                        
                        # Convert AirPlay volume (-144 to 0) to 0-100%
                        if airplay_vol <= -144:
                            volume = 0
                            self._airplay_muted = True
                        else:
                            # Linear conversion: -30 to 0 -> 0 to 100
                            # AirPlay typically uses -30 to 0 range
                            volume = max(0, min(100, int((airplay_vol + 30) * 100 / 30)))
                            self._airplay_muted = False
                        
                        self._airplay_volume = volume
                        logger.info(f"AirPlay volume from phone: {airplay_vol} dB -> {volume}%")
                        
                        # Apply volume to PulseAudio sink-input
                        self._sync_volume_to_pulseaudio(volume)
                        
                        # Notify mixer of external volume change (updates HA)
                        if self.on_external_volume:
                            self.on_external_volume(self.name, volume)
                        else:
                            self._update_state(volume=volume)
                        
                except (ValueError, IndexError) as e:
                    logger.debug(f"Failed to parse volume: {data} - {e}")
            
            # Play start: ssnc pbeg
            elif type_str == 'ssnc' and code_str == 'pbeg':
                logger.info("AirPlay playback started")
            
            # Play end: ssnc pend
            elif type_str == 'ssnc' and code_str == 'pend':
                logger.info("AirPlay playback ended")
            
            # Track info: core asal (album), core asar (artist), core minm (title)
            elif type_str == 'core':
                if code_str == 'minm':
                    title = data.decode('utf-8', errors='ignore')
                    logger.debug(f"AirPlay track: {title}")
                elif code_str == 'asar':
                    artist = data.decode('utf-8', errors='ignore')
                    logger.debug(f"AirPlay artist: {artist}")
                    
        except Exception as e:
            logger.debug(f"Error processing metadata: {e}")

    def _sync_volume_to_pulseaudio(self, volume: int) -> None:
        """Sync volume from phone to PulseAudio sink-input."""
        if not self._sink_input_id:
            info = self._find_airplay_sink_input()
            if info:
                self._sink_input_id = info.get("id")
        
        if self._sink_input_id:
            self._set_sink_input_volume(self._sink_input_id, volume)
            logger.debug(f"Synced AirPlay volume {volume}% to PulseAudio")

    def _run(self) -> None:
        """Main polling loop for PulseAudio state."""
        while self._running:
            try:
                self._poll_state()
            except Exception as e:
                logger.debug(f"AirPlay poll error: {e}")
            
            time.sleep(self._poll_interval)

    def _find_airplay_sink_input(self) -> dict | None:
        """Find AirPlay sink-input (shairport-sync)."""
        for pattern in self.SINK_INPUT_PATTERNS:
            info = self._get_sink_input_info(pattern)
            if info:
                return info
        return None

    def _poll_state(self) -> None:
        """Poll current state from PulseAudio."""
        info = self._find_airplay_sink_input()
        
        if not info:
            # AirPlay not active
            if self._state.state != PlaybackState.IDLE:
                self._update_state(
                    state=PlaybackState.IDLE,
                    level=0,
                )
            self._sink_input_id = None
            return
        
        self._sink_input_id = info.get("id")
        
        # Determine playback state from corked status
        corked = info.get("corked", False)
        state = PlaybackState.PAUSED if corked else PlaybackState.PLAYING
        
        # Read actual audio volume from PulseAudio (since we control it)
        volume = info.get("volume", self._airplay_volume)
        muted = info.get("muted", False)
        
        self._update_state(
            state=state,
            volume=int(volume),
            muted=muted,
        )

    def is_active(self) -> bool:
        """Check if AirPlay is playing."""
        return self._state.state == PlaybackState.PLAYING

    def get_volume(self) -> int:
        """Get volume."""
        return self._state.volume

    def set_volume(self, volume: int) -> bool:
        """Set volume via PulseAudio sink-input.
        
        Note: This sets PulseAudio volume, not AirPlay protocol volume.
        The phone controls AirPlay volume, but we can adjust output via PA.
        """
        volume = max(0, min(100, volume))
        
        if not self._sink_input_id:
            info = self._find_airplay_sink_input()
            if info:
                self._sink_input_id = info.get("id")
        
        if self._sink_input_id:
            success = self._set_sink_input_volume(self._sink_input_id, volume)
            if success:
                self._airplay_volume = volume
                self._update_state(volume=volume)
                return True
        
        # Still update state for HA even if PA failed
        self._airplay_volume = volume
        self._update_state(volume=volume)
        return True

    def get_muted(self) -> bool:
        """Get mute state."""
        return self._state.muted

    def set_muted(self, muted: bool) -> bool:
        """Set mute via sink-input."""
        # Always refresh sink-input ID
        info = self._find_airplay_sink_input()
        if info:
            self._sink_input_id = info.get("id")
        
        if not self._sink_input_id:
            logger.warning("No AirPlay sink-input found for mute")
            return False
        
        logger.info(f"Setting AirPlay mute={muted} on sink-input {self._sink_input_id}")
        success = self._set_sink_input_mute(self._sink_input_id, muted)
        if success:
            self._update_state(muted=muted)
            logger.info(f"AirPlay mute set to {muted}")
        else:
            logger.warning(f"Failed to set AirPlay mute on sink-input {self._sink_input_id}")
        return success

    def get_level(self) -> int:
        """Get current audio level (stub)."""
        if self._state.state == PlaybackState.PLAYING and not self._state.muted:
            return 50
        return 0

    # AirPlay doesn't support playback control from our end
    def play(self) -> bool:
        return False

    def pause(self) -> bool:
        return False

    def stop_playback(self) -> bool:
        return False
