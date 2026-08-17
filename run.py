from music_video_producer.app import app
from music_video_producer.config import Settings

if __name__ == "__main__":
    import uvicorn

    settings = Settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
