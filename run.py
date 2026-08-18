from music_video_producer.app import app
from music_video_producer.config import Settings

if __name__ == "__main__":
    import logging

    import uvicorn

    # uvicorn configures its own loggers and leaves the root logger at WARNING, so an
    # application `logger.info` never reaches the console and a `logger.warning` arrives
    # through the bare last-resort handler with no name attached. The VRAM eject reports
    # what it did that way — including the line confirming a model was released — so
    # without this the feature would be invisible in exactly the situation it is meant to
    # be observed in. Scoped to this package rather than the root logger so httpx's
    # per-request INFO chatter, one line per ComfyUI poll, stays out of the log.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    package_logger = logging.getLogger("music_video_producer")
    package_logger.setLevel(logging.INFO)
    package_logger.addHandler(handler)
    package_logger.propagate = False

    settings = Settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
