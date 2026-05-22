from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import traceback
import warnings
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import truststore


if __name__ == "__main__":
    truststore.inject_into_ssl()

    from src.config import FILE_FORMATTER
    from src.config.settings import Settings
    from src.core.client import Twitch
    from src.exceptions import CaptchaRequired
    from src.i18n import _
    from src.utils.log_redaction import SecretRedactingFilter
    from src.version import __version__

    logger = logging.getLogger("TwitchDrops")
    if logger.level < logging.INFO:
        logger.setLevel(logging.INFO)
    # Always add console handler. The redaction filter is attached at handler
    # level so it catches log records from every child logger that propagates
    # to the TwitchDrops handlers (gql, websocket, etc.).
    secret_filter = SecretRedactingFilter()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(FILE_FORMATTER)
    console_handler.addFilter(secret_filter)
    logger.addHandler(console_handler)

    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "TDM.log"

    # Add file handler for timestamped log
    file_handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=5)
    file_handler.setFormatter(FILE_FORMATTER)
    file_handler.addFilter(secret_filter)
    logger.addHandler(file_handler)

    logger.info("Logger initialized")

    warnings.simplefilter("default", ResourceWarning)

    logger.debug("Loading settings")
    try:
        settings = Settings()
    except Exception:
        logger.exception("Error while loading settings")
        print(f"Settings error: {traceback.format_exc()}", file=sys.stderr)
        sys.exit(4)

    # client run
    async def main():
        # set language
        if settings.language:
            _.set_language(settings.language)

        logger.info("=== TwitchDropsMiner Starting ===")
        logger.info(f"Version: {__version__}")
        logger.info(f"Python version: {sys.version}")
        logger.info(f"Platform: {sys.platform}")
        logger.info(f"Proxy: {settings.proxy}")
        logger.info(f"Language: {settings.language}")
        logger.info(
            f"Minimum refresh interval: {settings.minimum_refresh_interval_minutes} minutes"
        )

        exit_status = 0
        client = Twitch(settings)

        # Initialize web GUI
        from src.web import app as webapp
        from src.web.gui_manager import WebGUIManager

        # Set up web GUI
        client.gui = WebGUIManager(client)
        # Set up webapp references
        webapp.set_managers(client.gui, client)
        # Start web server in background. Default to loopback; opt in to LAN
        # exposure by setting TDM_HOST=0.0.0.0 (or any other interface).
        bind_host = os.environ.get("TDM_HOST", "127.0.0.1")
        bind_port = int(os.environ.get("TDM_PORT", "8080"))
        logger.info(f"Starting web server on http://{bind_host}:{bind_port}")

        # Generate / load the API session token and print the bootstrap URL
        # operators must open once on any non-loopback browser to install the
        # session cookie. On loopback the cookie is installed automatically.
        from src.auth.api_token import bootstrap_url, load_or_create_token

        api_token = load_or_create_token()
        logger.info(
            "Bootstrap URL (open once from a remote browser): "
            + bootstrap_url(bind_host, bind_port, api_token)
        )
        web_server_task = asyncio.create_task(webapp.run_server(host=bind_host, port=bind_port))

        loop = asyncio.get_running_loop()
        if sys.platform == "linux":
            logger.debug("Setting up signal handlers for SIGINT and SIGTERM")
            loop.add_signal_handler(signal.SIGINT, lambda *_: client.close())
            loop.add_signal_handler(signal.SIGTERM, lambda *_: client.close())

        logger.info("Starting main client run loop")
        try:
            await client.run()
            logger.info("Client run completed normally")
        except CaptchaRequired:
            logger.error("Captcha required - cannot continue")
            exit_status = 1
            client.print(_.t["error"]["captcha"])
        except Exception:
            logger.exception("Fatal error encountered during client run")
            exit_status = 1
            client.print("Fatal error encountered:\n")
            client.print(traceback.format_exc())
        finally:
            logger.info("=== Starting shutdown sequence ===")
            if sys.platform == "linux":
                logger.debug("Removing signal handlers (Linux)")
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            logger.info("Notifying client of exit")
            client.print(_.t["gui"]["status"]["exiting"])
            # Shutdown web server
            if web_server_task and not web_server_task.done():
                logger.info("Shutting down web server")
                # Trigger graceful shutdown and wait for it to finish
                await webapp.shutdown_server()
                # Wait for server to actually exit (with timeout)
                try:
                    await asyncio.wait_for(web_server_task, timeout=5.0)
                    logger.info("Web server task completed gracefully")
                except asyncio.TimeoutError:
                    logger.warning("Web server didn't exit in time, forcing cancellation")
                    web_server_task.cancel()
                    try:
                        await web_server_task
                    except asyncio.CancelledError:
                        logger.info("Web server task force-cancelled")
                except Exception as e:
                    logger.error(f"Error while shutting down web server: {e}")
            else:
                logger.debug(
                    f"Web server task status: task={web_server_task is not None}, done={web_server_task.done() if web_server_task else 'N/A'}"
                )
            logger.info("Shutting down Twitch client")
            await client.shutdown()
            logger.info("Twitch client shutdown completed")
        logger.info(f"Shutdown complete - exit_status={exit_status}")
        if exit_status != 0:
            logger.warning("Application terminated with error - showing error state")
            # Application terminated with error
            client.print(_.t["status"]["terminated"])
            client.gui.status.update(_.t["gui"]["status"]["terminated"])
            # notify the user about the closure
            client.gui.grab_attention(sound=True)
            # Web GUI doesn't need to wait - browser clients can stay connected
            logger.info("Web GUI - no need to wait for user to close browser")
        else:
            logger.info("Normal shutdown - proceeding")
        # save the application state
        logger.info("Saving application state")
        settings.save()
        logger.info("Application state saved")
        logger.info(f"=== Exiting with status code: {exit_status} ===")
        sys.exit(exit_status)

    asyncio.run(main())
