from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repo"
    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repo_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    default_branch: Mapped[str] = mapped_column(String(128))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    files: Mapped[list["File"]] = relationship(back_populates="repo", cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "file"
    __table_args__ = (UniqueConstraint("repo_id", "path", name="uq_file_repo_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    language: Mapped[str | None] = mapped_column(String(32))

    repo: Mapped[Repo] = relationship(back_populates="files")
