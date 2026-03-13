"""
Database engine y session — sync SQLAlchemy con psycopg2.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from fastapi import Depends
from core.config import settings

engine = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Depends(get_db)
