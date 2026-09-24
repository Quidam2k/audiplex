from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw, ImageFont

from .bus import BusClient
from .player import Player
from .proxy import AuthProxy


logger = logging.getLogger(__name__)


@dataclass
class Config:
    base_url: str
    token: str
    device_id: str
    device_name: str


def _app_directory() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AudiplexPC"
    return Path.home() / "AppData" / "Roaming" / "AudiplexPC"


def _configure_logging(debug: bool) -> None:
    app_directory = _app_directory()
    app_directory.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        app_directory / "audiplex_pc.log",
        maxBytes=1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    )

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        handlers=[handler],
        force=True,
    )
    # --debug is for our code; these libraries flood the log at DEBUG.
    for noisy in ("httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config(path: str | None) -> Config:
    config_path = (
        Path(os.path.expandvars(os.path.expanduser(path)))
        if path
        else _app_directory() / "config.json"
    )

    values: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"Could not read configuration file {config_path}: {exc}"
            logger.error(message)
            raise SystemExit(message) from exc

        if not isinstance(loaded, dict):
            message = f"Configuration file {config_path} must contain a JSON object."
            logger.error(message)
            raise SystemExit(message)

        values = loaded

    def configured_value(environment_name: str, key: str) -> str:
        environment_value = os.environ.get(environment_name)
        value = environment_value if environment_value is not None else values.get(key, "")
        return str(value).strip() if value is not None else ""

    hostname = socket.gethostname()
    base_url = configured_value("AUDIPLEX_PC_URL", "base_url").rstrip("/")
    token = configured_value("AUDIPLEX_PC_TOKEN", "token")
    device_id = configured_value("AUDIPLEX_DEVICE_ID", "device_id")
    device_name = configured_value("AUDIPLEX_DEVICE_NAME", "device_name")

    if not device_id:
        device_id = f"pc-{hostname.lower()}"
    if not device_name:
        device_name = hostname

    missing = []
    if not base_url:
        missing.append("base_url")
    if not token:
        missing.append("token")

    if missing:
        message = (
            f"Missing required configuration value(s): {', '.join(missing)}. "
            f"Set them in {config_path} or with AUDIPLEX_PC_URL and "
            "AUDIPLEX_PC_TOKEN."
        )
        logger.error(message)
        raise SystemExit(message)

    return Config(
        base_url=base_url,
        token=token,
        device_id=device_id,
        device_name=device_name,
    )


def _icon_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill="#1f6f78")

    font = ImageFont.load_default()
    bounds = draw.textbbox((0, 0), "A", font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    position = (
        (64 - width) / 2 - bounds[0],
        (64 - height) / 2 - bounds[1],
    )
    draw.text(position, "A", font=font, fill="white")
    return image


def _format_time(ms: int) -> str:
    seconds = max(0, int(ms) // 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _run(config: Config) -> None:
    proxy = AuthProxy(config.base_url, config.token)
    proxy_base = proxy.start()

    player = Player()
    bus = BusClient(
        config.base_url,
        config.token,
        config.device_id,
        config.device_name,
        player,
        proxy_base,
    )
    player.on_error = lambda event, message: bus.client_log(event, message)
    bus.start()

    root = tk.Tk()
    root.withdraw()

    tk_queue: queue.Queue[Callable[[], None]] = queue.Queue()
    cache_lock = threading.Lock()
    shutdown_started = threading.Event()
    status_stop = threading.Event()
    cached_devices: dict[str, Any] = {}
    player_window: tk.Toplevel | None = None
    icon: pystray.Icon

    def post(callback: Callable[[], None]) -> None:
        tk_queue.put(callback)

    def status_text() -> str:
        if not bus.connected:
            return "Offline"

        with cache_lock:
            snapshot = cached_devices

        active_device = snapshot.get("active_device_id")
        if active_device == config.device_id:
            return "Active here"
        if active_device == "phone":
            return "Standby (phone active)"
        return "Standby"

    def update_icon_menu() -> None:
        try:
            icon.update_menu()
        except Exception:
            logger.debug("Could not update tray menu", exc_info=True)

    def refresh_status() -> None:
        nonlocal cached_devices

        try:
            devices = bus.devices()
        except Exception as exc:
            logger.warning("Could not refresh device status: %s", exc)
        else:
            with cache_lock:
                cached_devices = devices
        finally:
            update_icon_menu()

    def status_loop() -> None:
        while not status_stop.is_set():
            refresh_status()
            if status_stop.wait(10):
                break

    def run_player_action(action: Callable[[], None]) -> None:
        try:
            action()
        except Exception:
            logger.exception("Player action failed")

    def transfer(device_id: str) -> None:
        def worker() -> None:
            try:
                bus.activate(device_id)
            except Exception:
                logger.exception("Could not activate device %s", device_id)
            finally:
                refresh_status()

        threading.Thread(
            target=worker,
            name=f"audiplex-transfer-{device_id}",
            daemon=True,
        ).start()

    def show_player() -> None:
        nonlocal player_window

        if player_window is not None:
            try:
                if player_window.winfo_exists():
                    player_window.deiconify()
                    player_window.lift()
                    return
            except tk.TclError:
                pass
            player_window = None

        window = tk.Toplevel(root)
        player_window = window
        window.title(f"Audiplex - {config.device_name}")
        window.geometry("360x170")
        window.resizable(False, False)

        title_var = tk.StringVar(value="Nothing playing")
        artist_var = tk.StringVar(value="")
        position_var = tk.StringVar(value="0:00 / 0:00")
        status_var = tk.StringVar(value=status_text())

        tk.Label(
            window,
            textvariable=title_var,
            font=("Segoe UI", 11, "bold"),
            anchor="center",
        ).pack(fill="x", padx=10, pady=(10, 1))
        tk.Label(window, textvariable=artist_var, anchor="center").pack(
            fill="x", padx=10
        )
        tk.Label(window, textvariable=position_var, anchor="center").pack(
            fill="x", padx=10, pady=(4, 0)
        )
        tk.Label(window, textvariable=status_var, anchor="center").pack(
            fill="x", padx=10, pady=(2, 5)
        )

        buttons = tk.Frame(window)
        buttons.pack(fill="x", padx=6, pady=(2, 8))

        tk.Button(
            buttons,
            text="Prev",
            command=lambda: run_player_action(player.previous),
        ).pack(side="left", expand=True, padx=2)
        tk.Button(
            buttons,
            text="Play/Pause",
            command=lambda: run_player_action(player.toggle),
        ).pack(side="left", expand=True, padx=2)
        tk.Button(
            buttons,
            text="Skip",
            command=lambda: run_player_action(player.skip),
        ).pack(side="left", expand=True, padx=2)
        tk.Button(
            buttons,
            text="Transfer here",
            command=lambda: transfer(config.device_id),
        ).pack(side="left", expand=True, padx=2)
        tk.Button(
            buttons,
            text="To phone",
            command=lambda: transfer("phone"),
        ).pack(side="left", expand=True, padx=2)

        def close_window() -> None:
            nonlocal player_window
            player_window = None
            window.destroy()

        def refresh_window() -> None:
            if player_window is not window:
                return

            try:
                state = player.state()
                track = state["track"] or {}
                title = track.get("title")
                artist = track.get("artist")
                position = state["position_ms"]
                duration = state["duration_ms"]

                title_var.set(str(title) if title else "Nothing playing")
                artist_var.set(str(artist) if artist else "")
                position_var.set(
                    f"{_format_time(position)} / {_format_time(duration)}"
                )
                status_var.set(status_text())
            except Exception:
                logger.debug("Could not refresh player window", exc_info=True)

            try:
                window.after(1000, refresh_window)
            except tk.TclError:
                pass

        window.protocol("WM_DELETE_WINDOW", close_window)
        refresh_window()

    def shutdown() -> None:
        if shutdown_started.is_set():
            return

        shutdown_started.set()
        status_stop.set()

        for name, stop in (
            ("bus", bus.stop),
            ("player", player.stop),
            ("proxy", proxy.stop),
            ("tray icon", icon.stop),
        ):
            try:
                stop()
            except Exception:
                logger.exception("Could not stop %s", name)

        root.quit()

    def drain_tk_queue() -> None:
        while True:
            try:
                callback = tk_queue.get_nowait()
            except queue.Empty:
                break

            try:
                callback()
            except Exception:
                logger.exception("Queued UI action failed")

        if not shutdown_started.is_set():
            root.after(200, drain_tk_queue)

    def tray_status(_item: pystray.MenuItem) -> str:
        return status_text()

    def tray_show(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        post(show_player)

    def tray_toggle(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        run_player_action(player.toggle)

    def tray_skip(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        run_player_action(player.skip)

    def tray_transfer_here(
        _icon: pystray.Icon,
        _item: pystray.MenuItem,
    ) -> None:
        transfer(config.device_id)

    def tray_transfer_phone(
        _icon: pystray.Icon,
        _item: pystray.MenuItem,
    ) -> None:
        transfer("phone")

    def tray_quit(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        post(shutdown)

    menu = pystray.Menu(
        pystray.MenuItem(tray_status, None, enabled=False),
        pystray.MenuItem("Show player", tray_show, default=True),
        pystray.MenuItem("Play/Pause", tray_toggle),
        pystray.MenuItem("Skip", tray_skip),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Transfer here", tray_transfer_here),
        pystray.MenuItem("Transfer to phone", tray_transfer_phone),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", tray_quit),
    )

    icon = pystray.Icon(
        "audiplex_pc",
        _icon_image(),
        f"Audiplex - {config.device_name}",
        menu,
    )

    root.after(200, drain_tk_queue)
    icon.run_detached()

    threading.Thread(
        target=status_loop,
        name="audiplex-device-status",
        daemon=True,
    ).start()

    try:
        root.mainloop()
    finally:
        shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    try:
        parser = argparse.ArgumentParser(description="Audiplex PC renderer")
        parser.add_argument("--config", help="Path to the JSON configuration file")
        parser.add_argument(
            "--debug",
            action="store_true",
            help="Enable debug logging",
        )
        args = parser.parse_args()

        _configure_logging(args.debug)
        config = load_config(args.config)
        _run(config)
    except SystemExit:
        raise
    except Exception:
        logger.exception("Audiplex PC stopped because of an unexpected error")


if __name__ == "__main__":
    main()

