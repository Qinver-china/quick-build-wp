from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://quickbuild:quickbuild@localhost:5432/quickbuild"
    redis_url: str = "redis://localhost:6379/0"
    app_secret: str = "dev-secret-change-in-production"
    cors_origins: str = "http://localhost:5173"
    task_expire_hours: int = 24
    rate_limit_per_hour: int = 5

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
