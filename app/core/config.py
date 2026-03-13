"""  
Configuración centralizada del proyecto usando Pydantic Settings.
Carga todas las variables de entorno desde os.environ (Cloud Run) o .env (local)
"""
from pydantic_settings import BaseSettings
from pydantic import PostgresDsn
from pydantic_core import MultiHostUrl
from typing import Literal
import os

class Settings(BaseSettings):
    """Configuración centralizada de variables de entorno."""
    # Twilio
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_PHONE_NUMBER: str
    # Server
    SERVER_HOST: str = "localhost:8000"
    # Speech to Text - Google Service Account Credentials
    GOOGLE_PROJECT_ID: str
    GOOGLE_PRIVATE_KEY_ID: str
    GOOGLE_PRIVATE_KEY: str
    GOOGLE_CLIENT_EMAIL: str
    GOOGLE_CLIENT_ID: str
    GOOGLE_AUTH_URI: str = "https://accounts.google.com/o/oauth2/auth"
    GOOGLE_TOKEN_URI: str = "https://oauth2.googleapis.com/token"
    GOOGLE_AUTH_PROVIDER_CERT_URL: str = "https://www.googleapis.com/oauth2/v1/certs"
    GOOGLE_CLIENT_CERT_URL: str
    GOOGLE_UNIVERSE_DOMAIN: str = "googleapis.com"
    # LLM - OpenAI
    OPENAI_API_KEY: str
    # Text to Speech
    ELEVEN_API_KEY: str
    GATEWAY_URL: str = "http://localhost:8000"
    # Database
    POSTGRESQL_USER: str
    POSTGRESQL_PASSWORD: str
    POSTGRESQL_SERVER: str
    POSTGRESQL_PORT: int
    POSTGRESQL_DB: str

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn:
        return MultiHostUrl.build(
            scheme="postgresql+psycopg2",
            username=self.POSTGRESQL_USER,
            password=self.POSTGRESQL_PASSWORD,
            host=self.POSTGRESQL_SERVER,
            port=self.POSTGRESQL_PORT,
            path=self.POSTGRESQL_DB,
        )

    # Google Credentials as Dict
    @property
    def GOOGLE_CREDENTIALS_DICT(self) -> dict:
        return {
            "type": "service_account",
            "project_id": self.GOOGLE_PROJECT_ID,
            "private_key_id": self.GOOGLE_PRIVATE_KEY_ID,
            "private_key": self.GOOGLE_PRIVATE_KEY,
            "client_email": self.GOOGLE_CLIENT_EMAIL,
            "client_id": self.GOOGLE_CLIENT_ID,
            "auth_uri": self.GOOGLE_AUTH_URI,
            "token_uri": self.GOOGLE_TOKEN_URI,
            "auth_provider_x509_cert_url": self.GOOGLE_AUTH_PROVIDER_CERT_URL,
            "client_x509_cert_url": self.GOOGLE_CLIENT_CERT_URL,
            "universe_domain": self.GOOGLE_UNIVERSE_DOMAIN,
        }
    # Environment Settings
    ENVIRONMENT: Literal["local", "qa", "production"] = "local"
    DEBUG: bool = True
    
    class Config:
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
