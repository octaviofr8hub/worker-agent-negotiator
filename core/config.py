"""
Configuración centralizada del proyecto usando Pydantic Settings.
Carga todas las variables de entorno desde os.environ (Cloud Run) o .env (local)
"""
from pydantic_settings import BaseSettings
from typing import Literal, Optional
import os


class Settings(BaseSettings):
    """Configuración centralizada de variables de entorno."""
    
    # LIVEKIT Configuration
    # En LiveKit Cloud estas 3 se inyectan automáticamente; en local se leen del .env
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""
    SIP_OUTBOUND_TRUNK_ID: str
    
    # Speech to Text - Google Service Account Credentials (JSON fields as env vars)
    
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
    
    # LLM APIs
    OPENAI_API_KEY: str
    #ASI1_API_KEY: Optional[str] = None
    
    # Text to Speech
    ELEVEN_API_KEY: str
    GATEWAY_URL: str = "http://localhost:8000"
    
    # FR8APP APIs
    AUTH_FR8APP: str
    UPDATE_FR8APP: str
    EMAIL_FR8APP: str
    PASSWORD_FR8APP: str
    
    # Google Credentials as Dict (construido desde env vars)
    @property
    def GOOGLE_CREDENTIALS_DICT(self) -> dict:
        """Construye el diccionario de credenciales de Google desde variables de entorno."""
        return {
            "type": "service_account",
            "project_id": self.GOOGLE_PROJECT_ID,
            "private_key_id": self.GOOGLE_PRIVATE_KEY_ID,
            "private_key": self.GOOGLE_PRIVATE_KEY,
            "client_email": self.GOOGLE_CLIENT_EMAIL,
            "client_id": self.GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": self.GOOGLE_CLIENT_CERT_URL,
            "universe_domain": "googleapis.com",
        }
    
    # Environment Settings
    ENVIRONMENT: Literal["local", "qa", "production"] = "local"
    DEBUG: bool = True
    
    class Config:
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
