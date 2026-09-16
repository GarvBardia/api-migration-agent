"""sqlalchemy.ext.declarative.declarative_base was moved to
sqlalchemy.orm.declarative_base in SQLAlchemy 1.4 -- the old import path
is deprecated (removed outright in some 2.x-style-only setups)."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    name = Column(String)
