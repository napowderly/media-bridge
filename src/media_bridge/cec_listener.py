"""
HDMI-CEC listener using cec-ctl (Linux kernel CEC API).

Uses cec-ctl for CEC communication and pactl for PulseAudio volume control.
Based on proven bash script approach for RPi5 vc4-hdmi.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class CECState:
    """Current state from CEC."""
    tv_power: bool = False
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "tv_power": self.tv_power,
        }


class CECListener:
    """
    Listens to HDMI-CEC events using cec-ctl.
    
    Detects:
    - TV power state changes
    - Volume up/down/mute commands from TV remote
    
    Volume commands are passed to a callback for handling by the audio mixer.
    """

    def __init__(
        self,
        device: str = "/dev/cec0",
        on_state_change: Callable[[str, bool | int], None] | None = None,
        on_volume_command: Callable[[str], None] | None = None,
        poll_interval: float = 5.0,
        volume_step: int = 5,
    ):
        self.device = device
        self.on_state_change = on_state_change
        self.on_volume_command = on_volume_command  # Callback for volume_up/volume_down/mute
        self.poll_interval = poll_interval
        self.volume_step = volume_step
        
        self._state = CECState()
        self._lock = threading.Lock()
        self._running = False
        self._cec_thread: threading.Thread | None = None
        self._cec_available = False
        self._dev_num = "0"

    @property
    def state(self) -> CECState:
        with self._lock:
            return CECState(
                tv_power=self._state.tv_power,
                last_update=self._state.last_update,
            )

    def _get_dev_num(self) -> str:
        """Extract device number from device path."""
        match = re.search(r'cec(\d+)', self.device)
        return match.group(1) if match else "0"

    def _find_hdmi_edid(self) -> str | None:
        """Find connected HDMI EDID file for physical address."""
        drm_path = Path("/sys/class/drm")
        for d in drm_path.glob("*HDMI-A-*"):
            status_file = d / "status"
            if status_file.exists():
                try:
                    status = status_file.read_text().strip()
                    if status == "connected":
                        edid_file = d / "edid"
                        if edid_file.exists():
                            return str(edid_file)
                except Exception:
                    pass
        return None

    def _init_cec(self) -> bool:
        """Initialize CEC adapter - try Audio System first, fall back to Playback."""
        self._dev_num = self._get_dev_num()
        
        # Get physical address from EDID
        edid_file = self._find_hdmi_edid()
        
        if edid_file:
            logger.info(f"Using EDID from {edid_file}")
        
        try:
            # Step 1: Clear any existing logical addresses
            subprocess.run(
                ["cec-ctl", "-d", self._dev_num, "--clear"],
                capture_output=True,
                timeout=5,
            )
            
            # Step 2: Try to register as Audio System first (for volume control)
            audio_cmd = ["cec-ctl", "-d", self._dev_num, "--audio", "--osd-name", "Pi Audio"]
            if edid_file:
                audio_cmd.extend(["--phys-addr-from-edid", edid_file])
            
            subprocess.run(audio_cmd, capture_output=True, text=True, timeout=10)
            
            # Check if Audio System registration worked
            check = subprocess.run(
                ["cec-ctl", "-d", self._dev_num, "-l"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            la = check.stdout.strip().split('\n')[-1] if check.returncode == 0 else "15"
            
            if la == "5":
                logger.info("Registered as Audio System (address 5) - volume commands enabled")
                # Enable System Audio Mode
                subprocess.run(
                    ["cec-ctl", "-d", self._dev_num, "--set-system-audio-mode", "sys-aud-status=on"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                # Fall back to Playback device (this usually works)
                logger.info("Audio System not available, registering as Playback device")
                subprocess.run(
                    ["cec-ctl", "-d", self._dev_num, "--clear"],
                    capture_output=True,
                    timeout=5,
                )
                playback_cmd = ["cec-ctl", "-d", self._dev_num, "--playback", "--osd-name", "Pi Media"]
                if edid_file:
                    playback_cmd.extend(["--phys-addr-from-edid", edid_file])
                subprocess.run(playback_cmd, capture_output=True, text=True, timeout=10)
                
                # Still try to enable System Audio Mode (might work for some TVs)
                subprocess.run(
                    ["cec-ctl", "-d", self._dev_num, "--set-system-audio-mode", "sys-aud-status=on"],
                    capture_output=True,
                    timeout=5,
                )
                
                # Get final address
                check = subprocess.run(
                    ["cec-ctl", "-d", self._dev_num, "-l"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                la = check.stdout.strip().split('\n')[-1] if check.returncode == 0 else "?"
                logger.info(f"Registered as Playback device (address {la})")
            
            self._cec_available = True
            logger.info(f"CEC initialized on /dev/cec{self._dev_num}")
            return True
            
        except FileNotFoundError:
            logger.error("cec-ctl not found. Install with: sudo apt install v4l-utils")
            return False
        except subprocess.TimeoutExpired:
            logger.error("CEC initialization timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize CEC: {e}")
            return False

    def _update_power(self, power_on: bool) -> None:
        """Update TV power state."""
        with self._lock:
            if self._state.tv_power != power_on:
                self._state.tv_power = power_on
                self._state.last_update = time.time()
                logger.info(f"TV power changed: {power_on}")
                if self.on_state_change:
                    self.on_state_change("tv_power", power_on)

    def _poll_tv_power(self) -> None:
        """Poll TV power status via CEC."""
        if not self._cec_available:
            return
        
        try:
            # Send power status request to TV (address 0)
            result = subprocess.run(
                ["cec-ctl", "-d", self._dev_num, "--to", "0", "--give-device-power-status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            
            output = result.stdout + result.stderr
            output_lower = output.lower()
            
            # Parse power status from response (handles both "pwr-state" and "power-status")
            if "pwr-state: on" in output_lower or "power-status: on" in output_lower:
                self._update_power(True)
            elif any(x in output_lower for x in ["pwr-state: standby", "power-status: standby", 
                                                   "pwr-state: off", "power-status: off"]):
                self._update_power(False)
            # If no response or error, TV might be off or not responding
            elif "timed out" in output_lower or "not present" in output_lower or "failed" in output_lower:
                self._update_power(False)
            
            logger.debug(f"TV power poll result: {output[:100]}")
                
        except subprocess.TimeoutExpired:
            # Timeout usually means TV is off
            self._update_power(False)
        except Exception as e:
            logger.debug(f"Error polling TV power: {e}")

    def _run_cec_monitor(self) -> None:
        """Monitor CEC bus for events including volume commands."""
        logger.info("CEC monitor thread started")
        
        if not self._init_cec():
            logger.error("CEC init failed, monitor thread exiting")
            return
        
        while self._running:
            try:
                # Use --wait-for-msgs to receive CEC messages
                # Note: --monitor requires root, so we just use --wait-for-msgs
                proc = subprocess.Popen(
                    ["cec-ctl", "-d", self._dev_num, "--wait-for-msgs"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                
                logger.info("CEC monitor listening for messages...")
                
                for line in iter(proc.stdout.readline, ''):
                    if not self._running:
                        break
                    
                    line = line.strip()
                    if not line:
                        continue
                    
                    line_lower = line.lower()
                    
                    # Log CEC traffic at debug level
                    if "received" in line_lower or "user control" in line_lower:
                        logger.debug(f"CEC: {line}")
                    
                    # Handle volume controls - look for User Control Pressed messages
                    # Format: "User Control Pressed" or "USER_CONTROL_PRESSED"
                    if "user control pressed" in line_lower or "user_control_pressed" in line_lower:
                        if "volume up" in line_lower or "volume-up" in line_lower or "ui-cmd: volume up" in line_lower:
                            logger.info("CEC: Volume UP")
                            if self.on_volume_command:
                                self.on_volume_command("volume_up")
                        elif "volume down" in line_lower or "volume-down" in line_lower or "ui-cmd: volume down" in line_lower:
                            logger.info("CEC: Volume DOWN")
                            if self.on_volume_command:
                                self.on_volume_command("volume_down")
                        elif "mute" in line_lower or "mute-function" in line_lower or "restore-volume" in line_lower:
                            logger.info("CEC: Mute toggle")
                            if self.on_volume_command:
                                self.on_volume_command("mute_toggle")
                    
                    # Handle power status changes
                    if "report-power-status" in line_lower or "report power status" in line_lower:
                        if "pwr-state: on" in line_lower or "power-status: on" in line_lower:
                            self._update_power(True)
                        elif "pwr-state: standby" in line_lower or "power-status: standby" in line_lower:
                            self._update_power(False)
                    
                    # Handle standby broadcast
                    if "standby" in line_lower and ("received" in line_lower or ">>" in line):
                        self._update_power(False)
                    
                    # Handle active source / image view on (TV turning on)
                    if ("active source" in line_lower or "image view on" in line_lower) and "received" in line_lower:
                        self._update_power(True)
                
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                
            except Exception as e:
                logger.error(f"CEC monitor error: {e}")
            
            if self._running:
                logger.info("CEC monitor restarting in 2s...")
                time.sleep(2)
        
        logger.info("CEC monitor thread stopped")

    def _run_power_poll(self) -> None:
        """Periodically poll TV power status."""
        logger.info("TV power poll thread started")
        
        while self._running:
            try:
                self._poll_tv_power()
                time.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"TV power poll error: {e}")
                time.sleep(2.0)
        
        logger.info("TV power poll thread stopped")

    def start(self) -> None:
        """Start the CEC listener."""
        if self._running:
            return
        
        self._running = True
        
        # Start CEC monitor thread (handles messages and volume commands)
        self._cec_thread = threading.Thread(
            target=self._run_cec_monitor,
            name="cec-monitor",
            daemon=True,
        )
        self._cec_thread.start()
        
        # Start power poll thread (periodic TV power check)
        self._power_poll_thread = threading.Thread(
            target=self._run_power_poll,
            name="power-poll",
            daemon=True,
        )
        self._power_poll_thread.start()
        
        logger.info("CEC listener started")

    def stop(self) -> None:
        """Stop the CEC listener."""
        self._running = False
        
        if self._cec_thread:
            self._cec_thread.join(timeout=5.0)
            self._cec_thread = None
        
        if hasattr(self, '_power_poll_thread') and self._power_poll_thread:
            self._power_poll_thread.join(timeout=5.0)
            self._power_poll_thread = None
        
        logger.info("CEC listener stopped")

    # TV power control methods

    def tv_on(self) -> bool:
        """Turn TV on via CEC."""
        if not self._cec_available:
            return False
        
        try:
            subprocess.run(
                ["cec-ctl", "-d", self._dev_num, "--to", "0", "--image-view-on"],
                check=True,
                timeout=5,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to turn TV on: {e}")
            return False

    def tv_off(self) -> bool:
        """Turn TV off (standby) via CEC."""
        if not self._cec_available:
            return False
        
        try:
            subprocess.run(
                ["cec-ctl", "-d", self._dev_num, "--to", "0", "--standby"],
                check=True,
                timeout=5,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to turn TV off: {e}")
            return False

    # Aliases for main.py compatibility
    def power_on_tv(self) -> bool:
        """Alias for tv_on()."""
        return self.tv_on()

    def standby_tv(self) -> bool:
        """Alias for tv_off()."""
        return self.tv_off()
