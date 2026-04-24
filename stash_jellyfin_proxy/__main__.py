#!/usr/bin/env python3
"""Entry point for stash-jellyfin-stash_jellyfin_proxy.

All stash_jellyfin_proxy.* imports are deferred inside main() so _prescan_config_args can
inject CLI config paths into env vars before the bootstrap machinery reads
them at import time (bootstrap runs on stash_jellyfin_proxy.runtime's first import).
"""
import os
import sys
import logging
import asyncio
import signal
import argparse
import time
from logging.handlers import RotatingFileHandler


# Force UTF-8 on Windows consoles (cp1252 crashes on emoji log messages).
# Runs at import time so it fires before any logging output.
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def _prescan_config_args(argv):
    """Consume --config / --local-config from argv and promote to env vars.

    Must run before any stash_jellyfin_proxy.* import since bootstrap reads these env vars
    on the very first import of stash_jellyfin_proxy.runtime.
    """
    for flag, env_var in (("--config", "CONFIG_FILE"), ("--local-config", "LOCAL_CONFIG_FILE")):
        for i, arg in enumerate(argv):
            if arg == flag and i + 1 < len(argv):
                os.environ[env_var] = argv[i + 1]
                break
            if arg.startswith(flag + "="):
                os.environ[env_var] = arg.split("=", 1)[1]
                break


def main():
    _prescan_config_args(sys.argv[1:])

    try:
        from hypercorn.config import Config
        from hypercorn.asyncio import serve
    except ImportError as e:
        print(f"Missing dependency: {e}. Please run: pip install hypercorn starlette httpx Pillow")
        sys.exit(1)

    try:
        import setproctitle
        setproctitle.setproctitle("stash-jellyfin-proxy")
    except ImportError:
        pass

    # Default config location: project root (one level above this package).
    # Docker / production override via CONFIG_FILE env var.
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_pkg_dir)
    CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(_project_root, "stash_jellyfin_proxy.conf"))
    _base, _ext = os.path.splitext(CONFIG_FILE)
    LOCAL_CONFIG_FILE = os.getenv(
        "LOCAL_CONFIG_FILE",
        f"{_base}.local{_ext}" if _ext else f"{CONFIG_FILE}.local",
    )

    # Proxy imports deferred until env vars are in place.
    import stash_jellyfin_proxy.runtime as _runtime
    from stash_jellyfin_proxy.config.bootstrap import run_bootstrap
    run_bootstrap(CONFIG_FILE, LOCAL_CONFIG_FILE)

    from stash_jellyfin_proxy.logging_setup import setup_logging
    logger = setup_logging(
        log_level=_runtime.LOG_LEVEL,
        log_file=_runtime.LOG_FILE,
        log_dir=_runtime.LOG_DIR,
        log_max_size_mb=_runtime.LOG_MAX_SIZE_MB,
        log_backup_count=_runtime.LOG_BACKUP_COUNT,
    )

    from stash_jellyfin_proxy.state.stats import load_proxy_stats, save_proxy_stats
    from stash_jellyfin_proxy.stash.client import check_stash_connection
    from stash_jellyfin_proxy.app import app, ui_app, SuppressDisconnectFilter

    parser = argparse.ArgumentParser(
        prog="stash-jellyfin-proxy",
        description="Stash-Jellyfin Proxy Server — serve Stash over the Jellyfin API.",
    )
    parser.add_argument("--config", metavar="PATH", help="Path to base config file (default: stash_jellyfin_proxy.conf beside the script, or $CONFIG_FILE)")
    parser.add_argument("--local-config", metavar="PATH", help="Path to local override config merged on top of --config (default: <base>.local.conf, or $LOCAL_CONFIG_FILE)")
    parser.add_argument("--host", metavar="HOST", help="Override PROXY_BIND from config (e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, metavar="PORT", help="Override PROXY_PORT from config")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL from config")
    parser.add_argument("--debug", action="store_true", help="Shortcut for --log-level DEBUG")
    parser.add_argument("--no-log-file", action="store_true", help="Disable file logging")
    parser.add_argument("--no-ui", action="store_true", help="Disable Web UI server")
    args = parser.parse_args()

    if args.host:
        _runtime.PROXY_BIND = args.host
    if args.port:
        _runtime.PROXY_PORT = args.port
    if args.log_level:
        level = getattr(logging, args.log_level)
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)
    if args.debug:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
    if args.no_log_file:
        logger.handlers = [
            h for h in logger.handlers
            if not isinstance(h, (RotatingFileHandler, logging.FileHandler))
        ]

    logging.getLogger("hypercorn.error").addFilter(SuppressDisconnectFilter())
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    logger.info("--- Stash-Jellyfin Proxy v6.02 ---")

    if not check_stash_connection():
        logger.warning("Could not connect to Stash. Proxy will start but streaming will not work until Stash is reachable.")
        logger.warning(f"Check STASH_URL ({_runtime.STASH_URL}) and STASH_API_KEY settings.")

    _runtime.PROXY_RUNNING = True
    _runtime.PROXY_START_TIME = time.time()

    load_proxy_stats()

    proxy_config = Config()
    proxy_config.bind = [f"{_runtime.PROXY_BIND}:{_runtime.PROXY_PORT}"]
    proxy_config.accesslog = logging.getLogger("hypercorn.access")
    proxy_config.access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s'
    proxy_config.errorlog = logging.getLogger("hypercorn.error")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()
    _runtime.SHUTDOWN_EVENT = shutdown_event  # used by ui_api_restart

    def signal_handler():
        logger.info("Shutdown signal received...")
        save_proxy_stats()
        shutdown_event.set()

    async def run_servers():
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, signal_handler)

        tasks = [serve(app, proxy_config, shutdown_trigger=shutdown_event.wait)]

        if _runtime.UI_PORT > 0 and not args.no_ui:
            ui_config = Config()
            ui_config.bind = [f"{_runtime.PROXY_BIND}:{_runtime.UI_PORT}"]
            ui_config.accesslog = None
            ui_config.errorlog = logging.getLogger("hypercorn.error")
            tasks.append(serve(ui_app, ui_config, shutdown_trigger=shutdown_event.wait))
            logger.info(f"Web UI: http://{_runtime.PROXY_BIND}:{_runtime.UI_PORT}")

        logger.info("Starting Hypercorn server...")
        await asyncio.gather(*tasks)
        logger.info("Servers stopped.")

    try:
        loop.run_until_complete(run_servers())
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno == 98:  # Address already in use
            logger.error("ABORTING: Port already in use. Is another instance running?")
            logger.error(f"  Proxy port {_runtime.PROXY_PORT} or UI port {_runtime.UI_PORT} is already bound.")
            logger.error(f"  Try: lsof -i :{_runtime.PROXY_PORT} or lsof -i :{_runtime.UI_PORT}")
        else:
            logger.error(f"ABORTING: Network error: {e}")
        sys.exit(1)

    if getattr(_runtime, "RESTART_REQUESTED", False):
        logger.info("Executing restart...")
        time.sleep(0.5)

        in_docker = os.path.exists("/.dockerenv") or CONFIG_FILE.startswith("/config")
        if in_docker:
            logger.info("Docker detected - exiting for container restart")
            sys.exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
