"""Windows media keys and the media overlay (SMTC) for the PC client (#3921).

Optional: needs the winrt-* packages (windows_client/.venv). Without them
MediaKeys is a no-op, so a global-python run behaves as before.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

try:
    from winrt.windows.media import (
        MediaPlaybackStatus,
        MediaPlaybackType,
        SystemMediaTransportControlsButton,
    )
    from winrt.windows.media.playback import MediaPlayer
except Exception:  # winrt not installed (global-python run): media keys are simply off
    MediaPlayer = None

AVAILABLE = MediaPlayer is not None

logger = logging.getLogger(__name__)


class MediaKeys:
    def __init__(self, player: Any, on_action: Callable[[], None] | None = None) -> None:
        self.player = player
        self.on_action = on_action
        self._mp = None
        self._smtc = None
        self._token = None
        self._shown: tuple[str, str] | None = None
        self._actions: dict[Any, Callable[[], None]] | None = None
        self._unavailable_logged = False

    def start(self) -> bool:
        if not AVAILABLE:
            if not self._unavailable_logged:
                logger.info("winrt not installed; media keys off")
                self._unavailable_logged = True
            return False
        if self._smtc is not None:
            return True
        try:
            self._mp = MediaPlayer()
            # #3921: VLC plays the audio; drive the Windows overlay manually.
            self._mp.command_manager.is_enabled = False
            smtc = self._mp.system_media_transport_controls
            self._smtc = smtc
            smtc.is_enabled = True
            smtc.is_play_enabled = True
            smtc.is_pause_enabled = True
            smtc.is_next_enabled = True
            smtc.is_previous_enabled = True
            self._token = smtc.add_button_pressed(self._on_button)
            self.update()
            return True
        except Exception:
            logger.exception("Starting media keys failed")
            self.stop()
            return False

    def _on_button(self, sender: Any, args: Any) -> None:
        try:
            # #3921: Resolve enums lazily; SMTC has separate play/pause buttons.
            if self._actions is None:
                buttons = SystemMediaTransportControlsButton
                self._actions = {
                    buttons.PLAY: self.player.resume,
                    buttons.PAUSE: self.player.pause,
                    buttons.NEXT: self.player.skip,
                    buttons.PREVIOUS: self.player.previous,
                }
            action = self._actions.get(args.button)
            if action is None:
                return
            action()
        except Exception:
            logger.exception("Media key action failed")
        try:
            self.update()
        except Exception:
            logger.exception("Updating media keys after a button failed")
        try:
            if self.on_action is not None:
                self.on_action()
        except Exception:
            logger.exception("Media key callback failed")

    def update(self) -> None:
        smtc = self._smtc
        if smtc is None:
            return
        try:
            state = self.player.state()
            track, book = state.get("track"), state.get("book")
            if state["playing"]:
                smtc.playback_status = MediaPlaybackStatus.PLAYING
            elif track or book:
                smtc.playback_status = MediaPlaybackStatus.PAUSED
            else:
                smtc.playback_status = MediaPlaybackStatus.STOPPED

            if track:
                title, artist = track.get("title") or "", track.get("artist") or ""
            elif book:
                title, artist = book.get("title") or "", "Audiplex"
            else:
                title, artist = "", ""
            # #3921: Avoid refreshing unchanged metadata on every status tick.
            if (title, artist) != self._shown:
                du = smtc.display_updater
                du.type = MediaPlaybackType.MUSIC
                du.music_properties.title = title
                du.music_properties.artist = artist
                du.update()
                self._shown = (title, artist)
        except Exception:
            logger.warning("Updating media keys failed", exc_info=True)

    def stop(self) -> None:
        smtc = self._smtc
        self._smtc = None
        if smtc is not None:
            try:
                if self._token is not None:
                    smtc.remove_button_pressed(self._token)
            except Exception:
                logger.warning("Removing media key handler failed", exc_info=True)
            try:
                smtc.is_enabled = False
            except Exception:
                logger.warning("Disabling media keys failed", exc_info=True)
        self._token = None
        self._shown = None
        self._mp = None
