from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from user_service.database import Base


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "username"],
            ["users.id", "users.username"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bio: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    avatar_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    followee_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    user = relationship("User")


class Follow(Base):
    __tablename__ = "follows"

    follower_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    followee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
