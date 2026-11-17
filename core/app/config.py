from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ELLALON_")

    version: str = "0.1.0"
    database_url: str = "postgresql+psycopg://ellalon:ellalon@postgres:5432/ellalon"
    redis_url: str = "redis://redis:6379/0"
    docker_host: str = "unix:///var/run/docker.sock"
    docker_network: str = "ellalon_net"

    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 720
    module_token_ttl_days: int = 3650
    admin_user: str = "admin"
    admin_password: str = "admin"

    modules_dir: str = "/opt/ellalon/modules"
    web_dir: str = "/opt/ellalon/web"
    # HOST path used for module bind mounts (Docker daemon resolves it on the host)
    data_root: str = "/var/lib/ellalon"
    core_url: str = "http://ellalon-core:8000"
    reconcile_interval: int = 10
    heartbeat_ttl: int = 30


settings = Settings()
