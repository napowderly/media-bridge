"""
TV audio source via S/PDIF (eARC → DAC → ADC → Pi).

Handles:
- S/PDIF capture from ClearClick USB audio adapter
- Bitstream detection (AC3/EAC3/DTS) vs PCM
- Decoding bitstream audio via GStreamer + FFmpeg
- PCM passthrough for stereo audio
- Silence detection for auto-mute
- Volume/mute control via PulseAudio sink-input
- Level metering
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import signal
import struct
import subprocess
import threading
import time
from typing import Callable

from media_bridge.sources.base import AudioSource, PlaybackState, PulseAudioMixin

logger = logging.getLogger(__name__)


class TVSource(AudioSource, PulseAudioMixin):
    """
    TV audio source via S/PDIF capture.
    
    Replaces tv-auto-audio.sh with integrated media-bridge control.
    """

    DEFAULT_ALSA_DEVICE = "hw:CARD=ClearClick,DEV=0"
    DEFAULT_SILENCE_THRESHOLD_DB = -50
    DEFAULT_SILENCE_DURATION = 3.0  # Seconds before marking as idle
    PROBE_TIMEOUT = 2  # Seconds to probe for bitstream
    DELAY_MS = 120  # Audio delay for sync

    def __init__(
        self,
        on_state_change: Callable[[str, str, any], None] | None = None,
        on_external_volume: Callable[[str, int], None] | None = None,
        alsa_device: str | None = None,
        pulse_sink: str | None = None,
        poll_interval: float = 0.1,
        silence_threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
        silence_duration: float = DEFAULT_SILENCE_DURATION,
    ):
        super().__init__(name="tv", on_state_change=on_state_change, on_external_volume=on_external_volume)
        
        self._alsa_device = alsa_device or self.DEFAULT_ALSA_DEVICE
        self._pulse_sink = pulse_sink
        self._poll_interval = poll_interval
        
        self._thread: threading.Thread | None = None
        self._pipeline_proc: subprocess.Popen | None = None
        self._pipeline_type: str | None = None  # "bitstream" or "pcm"
        self._sink_input_id: str | None = None
        
        # Level metering
        self._current_level_db = -100.0
        self._last_sound_time = 0.0
        
        # Mute state (separate from sink-input mute for silence detection)
        self._user_muted = False
        
        # Silence detection settings (configurable via MQTT)
        self._silence_threshold_db = silence_threshold_db
        self._silence_duration = silence_duration
        
        # Auto-mute on silence
        self._auto_mute_enabled = True
        self._silence_muted = False  # True if we auto-muted due to silence
        
        # TV power state - pipeline only runs when TV is on
        self._tv_power_on = False
        self._pipeline_enabled = False
        
        # Pipeline restart debounce - prevent rapid restart on crashes
        self._last_pipeline_start = 0.0
        self._pipeline_restart_cooldown = 2.0  # Min seconds between pipeline starts
        
        # Prevent poll from overwriting just-set values (race condition fix)
        self._volume_set_time = 0.0
        self._mute_set_time = 0.0
        self._set_debounce = 0.5  # Ignore poll updates for 500ms after setting

    def start(self) -> None:
        """Start TV audio source."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="tv-source")
        self._thread.start()
        logger.info("TV source started")

    def set_tv_power(self, power_on: bool) -> None:
        """Set TV power state - controls whether pipeline runs."""
        if power_on == self._tv_power_on:
            return
        
        self._tv_power_on = power_on
        
        if power_on:
            logger.info("TV power ON - enabling audio pipeline")
            self._pipeline_enabled = True
        else:
            logger.info("TV power OFF - disabling audio pipeline")
            self._pipeline_enabled = False
            self._stop_pipeline()
            self._update_state(state=PlaybackState.IDLE, level=0, level_db=-100.0)

    def stop(self) -> None:
        """Stop TV audio source."""
        self._running = False
        self._stop_pipeline()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("TV source stopped")

    def _run(self) -> None:
        """Main loop - start pipeline and monitor."""
        # Wait for PulseAudio
        self._wait_for_pulse()
        
        # Find output sink
        if not self._pulse_sink:
            self._pulse_sink = self._find_scarlett_sink()
        
        if not self._pulse_sink:
            logger.error("No Scarlett sink found for TV audio")
            return
        
        while self._running:
            try:
                # Only run pipeline if TV is on
                if self._pipeline_enabled:
                    if not self._pipeline_proc or self._pipeline_proc.poll() is not None:
                        # Pipeline not running - check cooldown before restart
                        time_since_last = time.time() - self._last_pipeline_start
                        if time_since_last >= self._pipeline_restart_cooldown:
                            self._start_pipeline()
                        # Else: still in cooldown, wait
                    
                    # Poll for state updates (even during cooldown)
                    self._poll_state()
                else:
                    # TV is off - ensure pipeline is stopped
                    if self._pipeline_proc:
                        self._stop_pipeline()
                    # Still update state to show idle
                    if self._state.state != PlaybackState.IDLE:
                        self._update_state(state=PlaybackState.IDLE, level=0, level_db=-100.0)
                
            except Exception as e:
                logger.error(f"TV source error: {e}")
                self._stop_pipeline()
                time.sleep(1)
            
            time.sleep(self._poll_interval)

    def _wait_for_pulse(self, timeout: float = 30) -> bool:
        """Wait for PulseAudio to be available."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    ["pactl", "info"],
                    capture_output=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _find_scarlett_sink(self) -> str | None:
        """Find Scarlett Focusrite sink."""
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.split('\n'):
                if "Scarlett" in line and "analog-stereo" in line:
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        return parts[1]
        except Exception as e:
            logger.error(f"Error finding sink: {e}")
        return None

    def _probe_bitstream(self) -> str | None:
        """
        Probe S/PDIF input for bitstream codec (AC3/EAC3/DTS).
        Returns codec name or None if PCM.
        """
        if not shutil.which("ffprobe") or not shutil.which("arecord"):
            logger.warning("ffprobe or arecord not found, assuming PCM")
            return None
        
        try:
            # Capture ~1 second and test for S/PDIF bitstream
            cmd = f"""
                arecord -D '{self._alsa_device}' -f S16_LE -r 48000 -c 2 -t raw -d 1 2>/dev/null |
                head -c 192000 |
                ffprobe -hide_banner -loglevel error -f spdif -i pipe:0 \
                    -show_entries stream=codec_name -of csv=p=0 -select_streams a
            """
            
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT + 2,
            )
            
            codec = result.stdout.strip().split('\n')[0] if result.stdout.strip() else None
            
            if codec in ("ac3", "eac3", "dts"):
                logger.info(f"Detected bitstream codec: {codec}")
                return codec
            
        except subprocess.TimeoutExpired:
            logger.debug("Bitstream probe timed out")
        except Exception as e:
            logger.debug(f"Bitstream probe error: {e}")
        
        return None

    def _start_pipeline(self) -> None:
        """Start the appropriate audio pipeline (bitstream or PCM)."""
        self._stop_pipeline()
        self._last_pipeline_start = time.time()
        
        codec = self._probe_bitstream()
        
        if codec:
            self._start_bitstream_pipeline(codec)
        else:
            self._start_pcm_pipeline()

    def _start_bitstream_pipeline(self, codec: str) -> None:
        """Start GStreamer/FFmpeg pipeline for bitstream decoding."""
        if not shutil.which("gst-launch-1.0") or not shutil.which("ffmpeg"):
            logger.error("gst-launch-1.0 or ffmpeg not found")
            return
        
        logger.info(f"Starting bitstream pipeline for {codec}")
        
        # Pipeline: ALSA capture -> FFmpeg S/PDIF decode -> GStreamer -> PulseAudio
        # We use a shell pipeline with gst-launch for capture -> ffmpeg for decode -> gst-launch for output
        cmd = f"""
            gst-launch-1.0 -q alsasrc device="{self._alsa_device}" provide-clock=false \
                ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
                ! fdsink fd=1 2>/dev/null |
            ffmpeg -hide_banner -loglevel warning -f spdif -i pipe:0 -c:a copy -f ac3 - 2>/dev/null |
            gst-launch-1.0 -q fdsrc \
                ! audio/x-ac3,rate=48000,channels=2 \
                ! ac3parse ! avdec_ac3 ! audioconvert ! audioresample \
                ! identity ts-offset={self.DELAY_MS * 1000000} \
                ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
                ! pulsesink device="{self._pulse_sink}" sync=true
        """
        
        try:
            self._pipeline_proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            self._pipeline_type = "bitstream"
            logger.info("Bitstream pipeline started")
        except Exception as e:
            logger.error(f"Failed to start bitstream pipeline: {e}")

    def _start_pcm_pipeline(self) -> None:
        """Start PCM passthrough pipeline with level monitoring."""
        if not shutil.which("gst-launch-1.0"):
            logger.error("gst-launch-1.0 not found")
            return
        
        logger.info("Starting PCM passthrough pipeline")
        
        # Pipeline: ALSA capture -> tee -> (PulseAudio + level meter output)
        # Use tee to split audio: one path to PulseAudio, one to stdout for level metering
        cmd = [
            "gst-launch-1.0", "-q",
            "alsasrc", f"device={self._alsa_device}", "provide-clock=false",
            "!", "audio/x-raw,format=S16LE,rate=48000,channels=2",
            "!", "tee", "name=t",
            "t.", "!", "queue", "!", "audioconvert", "!", "audioresample",
            "!", f"pulsesink", f"device={self._pulse_sink}", "sync=true",
            "t.", "!", "queue", "max-size-buffers=2", "!", "fdsink", "fd=1",
        ]
        
        try:
            self._pipeline_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            self._pipeline_type = "pcm"
            
            # Start level metering thread
            threading.Thread(
                target=self._level_meter_loop,
                daemon=True,
                name="tv-level-meter",
            ).start()
            
            logger.info("PCM pipeline started with level metering")
        except Exception as e:
            logger.error(f"Failed to start PCM pipeline: {e}")

    def _stop_pipeline(self) -> None:
        """Stop the audio pipeline."""
        if self._pipeline_proc:
            try:
                # Kill process group
                os.killpg(os.getpgid(self._pipeline_proc.pid), signal.SIGTERM)
                self._pipeline_proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(self._pipeline_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self._pipeline_proc = None
            self._pipeline_type = None
            logger.info("Pipeline stopped")

    def _level_meter_loop(self) -> None:
        """Read audio samples from pipeline for level metering."""
        if not self._pipeline_proc or not self._pipeline_proc.stdout:
            return
        
        # Read in ~50ms chunks (48000 * 2ch * 2bytes * 0.05 = 9600 bytes)
        chunk_size = 9600
        
        while self._running and self._pipeline_proc:
            try:
                data = self._pipeline_proc.stdout.read(chunk_size)
                if not data:
                    break
                
                # Calculate RMS level in dB
                level_db = self._calculate_rms_db(data)
                self._current_level_db = level_db
                
                # Track last time we had sound
                if level_db > self._silence_threshold_db:
                    self._last_sound_time = time.time()
                
            except Exception as e:
                logger.debug(f"Level meter error: {e}")
                break

    def _calculate_rms_db(self, samples: bytes) -> float:
        """Calculate RMS level in dB from raw S16LE samples."""
        if len(samples) < 2:
            return -100.0
        
        sample_count = len(samples) // 2
        try:
            values = struct.unpack(f'<{sample_count}h', samples)
            square_sum = sum(v * v for v in values)
            rms = math.sqrt(square_sum / sample_count)
            if rms == 0:
                return -100.0
            return 20 * math.log10(rms / 32768.0)
        except Exception:
            return -100.0

    def _poll_state(self) -> None:
        """Update state based on pipeline and level."""
        # Find our sink-input
        self._sink_input_id = self._find_sink_input("gst-launch")
        
        if not self._sink_input_id:
            # No sink-input means pipeline not outputting
            if self._state.state != PlaybackState.IDLE:
                self._update_state(state=PlaybackState.IDLE, level=0)
            return
        
        # Get volume/mute from sink-input
        info = self._get_sink_input_info("gst-launch")
        if info:
            now = time.time()
            
            # Only update volume from poll if we didn't just set it (prevent race condition)
            if now - self._volume_set_time > self._set_debounce:
                volume = info.get("volume", 100)
            else:
                volume = self._state.volume  # Keep our just-set value
            
            # Determine state based on silence
            time_since_sound = time.time() - self._last_sound_time
            is_silent = self._current_level_db <= self._silence_threshold_db
            silence_exceeded = time_since_sound >= self._silence_duration
            
            if not is_silent:
                state = PlaybackState.PLAYING
                # Unmute if we had auto-muted due to silence
                if self._auto_mute_enabled and self._silence_muted:
                    logger.info("Audio detected, unmuting TV")
                    self._set_sink_input_mute(self._sink_input_id, self._user_muted)
                    self._silence_muted = False
            elif not silence_exceeded:
                state = PlaybackState.PLAYING  # Still "playing" during brief silence
            else:
                state = PlaybackState.IDLE  # Sustained silence
                # Auto-mute on sustained silence
                if self._auto_mute_enabled and not self._silence_muted and not self._user_muted:
                    logger.info(f"Silence detected ({self._current_level_db:.1f} dB < {self._silence_threshold_db} dB for {self._silence_duration}s), muting TV")
                    self._set_sink_input_mute(self._sink_input_id, True)
                    self._silence_muted = True
            
            # Determine effective mute state (respect debounce after user mute change)
            if now - self._mute_set_time > self._set_debounce:
                muted = info.get("muted", False) or self._user_muted or self._silence_muted
            else:
                muted = self._state.muted  # Keep our just-set value
            
            # Convert dB to 0-100 level
            # -50dB = 0, 0dB = 100
            level = max(0, min(100, int((self._current_level_db + 50) * 2)))
            if muted:
                level = 0
            
            # Also publish level_db for debugging
            level_db = round(self._current_level_db, 1)
            
            self._update_state(
                state=state,
                volume=volume,
                muted=muted,
                level=level,
                level_db=level_db,
            )

    def is_active(self) -> bool:
        """Check if TV audio is playing."""
        return self._state.state == PlaybackState.PLAYING

    def get_volume(self) -> int:
        """Get sink-input volume."""
        return self._state.volume

    def set_volume(self, volume: int) -> bool:
        """Set sink-input volume."""
        if not self._sink_input_id:
            self._sink_input_id = self._find_sink_input("gst-launch")
        
        if not self._sink_input_id:
            logger.warning("No TV sink-input found for volume")
            return False
        
        volume = max(0, min(100, volume))
        success = self._set_sink_input_volume(self._sink_input_id, volume)
        if success:
            self._volume_set_time = time.time()  # Prevent poll from overwriting
            self._update_state(volume=volume)
        return success

    def get_muted(self) -> bool:
        """Get mute state."""
        return self._state.muted

    def set_muted(self, muted: bool) -> bool:
        """Set mute via sink-input."""
        if not self._sink_input_id:
            self._sink_input_id = self._find_sink_input("gst-launch")
        
        if not self._sink_input_id:
            logger.warning("No TV sink-input found for mute")
            return False
        
        self._user_muted = muted
        success = self._set_sink_input_mute(self._sink_input_id, muted)
        if success:
            self._mute_set_time = time.time()  # Prevent poll from overwriting
            self._update_state(muted=muted)
        return success

    def get_level(self) -> int:
        """Get current audio level (0-100)."""
        return self._state.level

    # Silence detection settings

    def get_silence_threshold(self) -> int:
        """Get silence threshold in dB (negative value, e.g., -50)."""
        return int(self._silence_threshold_db)

    def set_silence_threshold(self, threshold_db: int) -> bool:
        """Set silence threshold in dB (should be negative, e.g., -50)."""
        # Clamp to reasonable range (-80 to -20 dB)
        threshold_db = max(-80, min(-20, threshold_db))
        self._silence_threshold_db = float(threshold_db)
        logger.info(f"Silence threshold set to {threshold_db} dB")
        self._notify("silence_threshold", threshold_db)
        return True

    def get_silence_duration(self) -> float:
        """Get silence duration in seconds."""
        return self._silence_duration

    def set_silence_duration(self, duration: float) -> bool:
        """Set silence duration in seconds."""
        # Clamp to reasonable range (0.5 to 30 seconds)
        duration = max(0.5, min(30.0, duration))
        self._silence_duration = duration
        logger.info(f"Silence duration set to {duration} seconds")
        self._notify("silence_duration", duration)
        return True

    def get_level_db(self) -> float:
        """Get current audio level in dB (for debugging)."""
        return self._current_level_db

    def get_auto_mute(self) -> bool:
        """Get auto-mute on silence setting."""
        return self._auto_mute_enabled

    def set_auto_mute(self, enabled: bool) -> bool:
        """Enable/disable auto-mute on silence."""
        self._auto_mute_enabled = enabled
        logger.info(f"Auto-mute on silence: {'enabled' if enabled else 'disabled'}")
        self._notify("auto_mute", enabled)
        
        # If disabling and we're currently silence-muted, unmute
        if not enabled and self._silence_muted and self._sink_input_id:
            self._set_sink_input_mute(self._sink_input_id, self._user_muted)
            self._silence_muted = False
        
        return True

    # TV doesn't support playback control from our end
    def play(self) -> bool:
        return False

    def pause(self) -> bool:
        return False

    def stop_playback(self) -> bool:
        return False

