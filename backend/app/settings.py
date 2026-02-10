from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ari_base_url: str
    ari_user: str
    ari_password: str
    ari_app: str

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

